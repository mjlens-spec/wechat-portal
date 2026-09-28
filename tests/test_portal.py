"""Unit tests on synthetic data only. No real WeChat records, keys or caches."""
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechat_portal import backend, common, documents, feishu, jobs, media, messages  # noqa: E402
from wechat_portal.common import Failure  # noqa: E402
from wechat_portal.portal import Portal  # noqa: E402

CHAT = "sample@chatroom"


def row(**updates):
    value = {"talker": CHAT, "server_id": 9223372036854775000, "sort_seq": 901, "create_time": 1790200000,
             "msg_type": 3, "sender": "sample", "content": {"Image": {"md5": "a" * 32}}}
    value.update(updates)
    return value


def file_row(**updates):
    content = {"File": {"title": "报价.pdf", "file_ext": "pdf", "file_size": 9, "md5": "b" * 32, "raw_xml": "<msg/>"}}
    return row(msg_type=49, content=content, **updates)


RECORD = """<recordinfo><title>群聊的聊天记录</title><datalist count="4">
<dataitem datatype="1"><sourcename>甲</sourcename><sourcetime>2026-9-24 10:00</sourcetime>
<datadesc>表在这 https://tenant.feishu.cn/wiki/AbCdEfGh123?sheet=Xy9&amp;from=im，请看</datadesc></dataitem>
<dataitem datatype="8"><sourcename>乙</sourcename><sourcetime>2026-9-24 10:01</sourcetime>
<datatitle>方案.pdf</datatitle><datasize>2048</datasize></dataitem>
<dataitem datatype="2"><sourcename>乙</sourcename><sourcetime>2026-9-24 10:02</sourcetime></dataitem>
<dataitem datatype="17"><sourcename>丙</sourcename><sourcetime>2026-9-24 10:03</sourcetime><datatitle>内层记录</datatitle>
<recordxml><recordinfo><title>内层</title><datalist count="1"><dataitem datatype="1"><sourcename>丁</sourcename>
<sourcetime>2026-9-23 09:00</sourcetime><datadesc>内层正文</datadesc></dataitem></datalist></recordinfo></recordxml></dataitem>
</datalist></recordinfo>"""


def merged_row(record=RECORD, title="聊天记录卡片标题"):
    raw = f"<msg><appmsg><title>{title}</title><type>19</type><recorditem><![CDATA[{record}]]></recorditem></appmsg></msg>"
    return row(msg_type=49, content={"MergedMessages": {"title": title, "raw_xml": raw}})


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.account = self.root / "account"
        (self.account / "db_storage").mkdir(parents=True)
        self.cache = self.root / "cache"
        (self.cache / "hardlink").mkdir(parents=True)
        (self.cache / "message").mkdir(parents=True)
        self.portal = Portal({"binary": "/nonexistent/wx-cli", "account_dir": str(self.account),
                              "cache_dir": str(self.cache)})
        self.portal.wx._cache_refreshed = True
        self.out = common.private_dir(self.root / "out")

    def tearDown(self):
        self.temporary.cleanup()

    def hardlink(self, table, rows):
        path = self.cache / "hardlink" / "hardlink.db"
        connection = sqlite3.connect(path)
        connection.execute("create table if not exists dir2id(username text primary key)")
        for name in ("video_hardlink_info_v4", "file_hardlink_info_v4"):
            connection.execute(f"create table if not exists {name}(md5 text, file_name text, file_size integer, dir1 integer)")
        for md5, name, size, directory in rows:
            connection.execute("insert or ignore into dir2id(username) values (?)", (directory,))
            dir_id = connection.execute("select rowid from dir2id where username=?", (directory,)).fetchone()[0]
            connection.execute(f"insert into {table} values (?,?,?,?)", (md5, name, size, dir_id))
        connection.commit()
        connection.close()


class SecurityTests(Base):
    def test_redaction_preserves_business_values(self):
        source = 'ROI 0.57; aeskey="secret-value" token=other-value\n<msg><img aeskey="image-secret"/></msg>'
        result = common.text_safe(source)
        self.assertIn("ROI 0.57", result)
        for secret in ("secret-value", "other-value", "image-secret"):
            self.assertNotIn(secret, result)

    def test_redaction_handles_quoted_json_values(self):
        result = common.text_safe('ROI 0.57 {"token": "secret with spaces", "image_aes_key": "private"}')
        self.assertIn("ROI 0.57", result)
        self.assertNotIn("secret with spaces", result)
        self.assertNotIn("private", result)

    def test_quotes_keep_reply_and_hide_image_xml(self):
        value = row(msg_type=49, content={"Quote": {"reply_text": "请修改这张表", "refer_sender": "同事",
                                                   "refer_content": '<?xml version="1.0"?><msg><img aeskey="secret"/></msg>',
                                                   "raw_xml": "must not escape"}})
        text = messages.message_text(value)
        self.assertIn("请修改这张表", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("must not escape", text)

    def test_stderr_diagnostics_do_not_escape(self):
        backend_result = subprocess.CompletedProcess([], 1, "", "permission denied aeskey=never-return-this")
        with patch.object(common.subprocess, "run", return_value=backend_result):
            with self.assertRaisesRegex(Failure, "^ACCESS_DENIED$"):
                common.invoke(["backend"])

    def test_missing_key_warnings_do_not_mask_real_error(self):
        stderr = ("  decrypt err message/message_resource.db: no matching enc_key found for this DB's salt\n"
                  "Cache: 0 decrypted, 21 cached, 3 errors\nError: contact \"某群\" not found")
        self.assertEqual(common.error_code(stderr), "CHAT_NOT_FOUND")
        self.assertEqual(common.error_code("error: no key found for account"), "KEY_UNAVAILABLE")

    def test_tmp_alias_is_supported(self):
        with tempfile.TemporaryDirectory(prefix="wechat-portal-test-", dir="/tmp") as folder:
            self.assertEqual(common.private_dir(folder), Path(folder).resolve())

    def test_private_output_does_not_overwrite(self):
        path = self.root / "output.png"
        common.write_new(path, b"original")
        with self.assertRaisesRegex(Failure, "OUTPUT_EXISTS"):
            common.write_new(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_read_rejects_symlink(self):
        actual = self.root / "actual.json"
        common.write_new(actual, b"{}")
        link = self.root / "link.json"
        link.symlink_to(actual)
        with self.assertRaises(OSError):
            common.private_read(link)

    def test_output_parent_must_be_private(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with self.assertRaisesRegex(Failure, "PRIVATE_DIRECTORY_REQUIRED"):
            common.output_stem(shared / "x")


class ReferenceTests(Base):
    def test_large_ids_roundtrip_without_float_loss(self):
        normalized = self.portal.normalize(row())
        self.assertEqual(normalized["message_id"], "9223372036854775000")
        payload = self.portal.unpack(normalized["media"]["ref"], "image")
        self.assertEqual(payload["server_id"], normalized["message_id"])

    def test_reference_rejects_other_account(self):
        token = self.portal.reference(row(), "image")
        self.portal.account_tag = "different"
        with self.assertRaisesRegex(Failure, "ACCOUNT_MISMATCH"):
            self.portal.unpack(token, "image")

    def test_reference_kind_must_match_command(self):
        token = self.portal.reference(row(), "image")
        with self.assertRaisesRegex(Failure, "REFERENCE_KIND_MISMATCH"):
            self.portal.unpack(token, "video")

    def test_legacy_or_incomplete_reference_is_rejected(self):
        data = {"v": 1, "account": self.portal.account_tag, "chat": "x", "server_id": "1", "sort_seq": "1", "timestamp": 1}
        token = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        with self.assertRaisesRegex(Failure, "REFERENCE_INVALID"):
            self.portal.unpack(token, "image")

    def test_same_second_message_requires_exact_identifier(self):
        expected = row()
        ref = self.portal.reference(expected, "image")
        different = row(server_id=9223372036854774999)
        with patch.object(self.portal.wx, "query", return_value={"items": [different]}):
            with self.assertRaisesRegex(Failure, "REFERENCE_NOT_FOUND"):
                self.portal.resolve(ref, "image")
        with patch.object(self.portal.wx, "query", return_value={"items": [expected]}):
            self.assertEqual(self.portal.resolve(ref, "image")["server_id"], expected["server_id"])

    def test_file_reference_rejects_non_file_app_message(self):
        link = row(msg_type=49, content={"Link": {"title": "x", "url": "https://a.example"}})
        ref = self.portal.reference(link, "file")
        with patch.object(self.portal.wx, "query", return_value={"items": [link]}):
            with self.assertRaisesRegex(Failure, "REFERENCE_NOT_FOUND"):
                self.portal.resolve(ref, "file")


class HistoryTests(Base):
    def test_chat_cannot_be_interpreted_as_native_option(self):
        with self.assertRaisesRegex(Failure, "CHAT_INVALID"):
            self.portal.history("--show-hidden")

    def test_date_range_is_inclusive_and_validated(self):
        start, end, _ = common.date_bounds("2026-09-24", "2026-09-24")
        self.assertEqual(dt.datetime.fromtimestamp(start).hour, 0)
        self.assertEqual(dt.datetime.fromtimestamp(end).strftime("%H:%M:%S"), "23:59:59")
        with self.assertRaisesRegex(Failure, "INVALID_DATE"):
            common.date_bounds("2026-02-31", None)

    def test_pagination_and_chronological_output(self):
        values = [row(sort_seq=902, create_time=1790200001), row()]
        with patch.object(self.portal.wx, "query", return_value={"items": values, "paging": {"has_more": True}}):
            result = self.portal.history(CHAT, offset=20)
        self.assertTrue(result["paging"]["has_more"])
        self.assertEqual(result["next_offset"], 22)
        self.assertLess(result["items"][0]["timestamp"], result["items"][1]["timestamp"])

    def test_mixed_conversations_are_not_combined(self):
        with patch.object(self.portal.wx, "query", return_value={"items": [row(), row(talker="other@chatroom")]}):
            with self.assertRaisesRegex(Failure, "CHAT_AMBIGUOUS"):
                self.portal.history("sample")

    def test_type_filter_keeps_only_requested_app_kind(self):
        items = [file_row(), row(msg_type=49, content={"Link": {"title": "t", "url": "https://x.example/a"}})]
        with patch.object(self.portal.wx, "query", return_value={"items": items}) as query:
            result = self.portal.history(CHAT, type_filter="file")
        self.assertIn("49", query.call_args[0][0])
        self.assertEqual([m["type"] for m in result["items"]], ["file"])
        self.assertEqual(result["next_offset"], 2)

    def test_file_message_is_normalized_with_media(self):
        message = self.portal.normalize(file_row())
        self.assertEqual(message["type"], "file")
        self.assertEqual(message["media"]["name"], "报价.pdf")
        self.assertEqual(message["media"]["ext"], "pdf")
        self.assertIn("[文件] 报价.pdf", message["text"])

    def test_merged_forward_is_expanded_with_links(self):
        message = self.portal.normalize(merged_row())
        forwarded = message["forwarded"]
        self.assertEqual(forwarded["count"], 4)
        self.assertEqual([i["type"] for i in forwarded["items"]], ["text", "file", "image", "merged"])
        self.assertIn("方案.pdf", forwarded["items"][1]["text"])
        self.assertEqual(forwarded["items"][3]["forwarded"]["items"][0]["text"], "内层正文")
        link = message["links"][0]
        self.assertEqual(link["url"], "https://tenant.feishu.cn/wiki/AbCdEfGh123?sheet=Xy9&from=im")
        self.assertEqual((link["feishu_type"], link["sheet_id"], link["readable"]), ("wiki", "Xy9", True))

    def test_merged_forward_title_falls_back_to_card(self):
        record = RECORD.replace("<title>群聊的聊天记录</title>", "")
        self.assertEqual(self.portal.normalize(merged_row(record))["forwarded"]["title"], "聊天记录卡片标题")

    def test_merged_forward_rejects_entity_declarations(self):
        evil = '<!DOCTYPE x [<!ENTITY a "aaaa">]><recordinfo><title>&a;</title></recordinfo>'
        self.assertIsNone(messages.parse_record(evil))


class LinkTests(unittest.TestCase):
    def test_urls_stop_at_cjk_and_trailing_punctuation(self):
        text = "看这个https://tenant.feishu.cn/docx/AbCdEf123这个文档。另见 (https://x.example/p?q=1)."
        self.assertEqual(messages.extract_urls(text),
                         ["https://tenant.feishu.cn/docx/AbCdEf123", "https://x.example/p?q=1"])

    def test_link_classification(self):
        cases = {
            "https://t.feishu.cn/docx/AbCdEf123": ("feishu", "docx", True),
            "https://t.feishu.cn/sheets/AbCdEf123?sheet=S1": ("feishu", "sheets", True),
            "https://t.feishu.cn/base/AbCdEf123": ("feishu", "base", False),
            "https://vc.feishu.cn/j/981971028": ("feishu", "meeting", False),
            "https://t.larksuite.com/wiki/AbCdEf123": ("feishu", "wiki", True),
        }
        for url, expected in cases.items():
            info = messages.classify_link(url)
            self.assertEqual((info["category"], info["feishu_type"], info["readable"]), expected, url)
        self.assertEqual(messages.classify_link("https://www.xiaohongshu.com/discovery/item/1")["category"], "xiaohongshu")
        self.assertEqual(messages.classify_link("https://mp.weixin.qq.com/s/abc")["category"], "wechat_article")
        # A look-alike host is not Feishu.
        self.assertEqual(messages.classify_link("https://feishu.cn.evil.example/docx/A")["category"], "web")


class MediaTests(Base):
    def attach(self, month, name):
        folder = self.account / "msg/attach" / hashlib.md5(CHAT.encode()).hexdigest() / month / "Img"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"synthetic")

    def test_image_quality_priority_across_months(self):
        for month, suffix in [("2026-09", "_t"), ("2026-08", "_h"), ("2026-09", "")]:
            self.attach(month, "a" * 32 + suffix + ".dat")
        found = media.image_candidates(self.account, CHAT, ["a" * 32])
        self.assertEqual([quality for _, quality in found], ["cached_hd", "cached_regular", "thumbnail"])

    def test_broken_hd_falls_back_and_reports_actual_quality(self):
        for suffix in ("_h", ""):
            self.attach("2026-09", "a" * 32 + suffix + ".dat")

        def decode(args):
            if args[2].endswith("_h.dat"):
                raise Failure("IMAGE_DECODE_FAILED")
            Path(args[-1]).write_bytes(b"GIF89a" + b"synthetic")
            return ""

        with patch.object(self.portal.wx, "raw", side_effect=decode), \
             patch.object(media, "invoke", return_value="pixelWidth: 800\npixelHeight: 600"):
            result = media.extract_image(self.portal.wx, row(), ["a" * 32], self.out / "image")
        self.assertEqual(result["quality"], "cached_regular")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["skipped_candidate_errors"], ["IMAGE_DECODE_FAILED"])
        self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)

    def test_packed_info_digest_extraction(self):
        blob = b"\x08\x01" + bytes([0x12, 0x22, 0x0A, 0x20]) + b"c" * 32 + b"\x10\x02"
        self.assertEqual(backend.packed_digests(blob), ["c" * 32])
        self.assertEqual(backend.packed_digests(b"xx" + b"d" * 32 + b"yy" + b"e" * 32), ["d" * 32, "e" * 32])
        self.assertEqual(backend.packed_digests(None), [])

    def test_message_digests_read_exact_row_from_cache(self):
        table = "Msg_" + hashlib.md5(CHAT.encode()).hexdigest()
        connection = sqlite3.connect(self.cache / "message" / "message_0.db")
        connection.execute(f'create table "{table}"(server_id integer, create_time integer, packed_info_data blob)')
        marker = bytes([0x12, 0x22, 0x0A, 0x20])
        connection.execute(f'insert into "{table}" values (?,?,?)', (5, 100, marker + b"f" * 32))
        connection.execute(f'insert into "{table}" values (?,?,?)', (5, 101, marker + b"9" * 32))
        connection.commit()
        connection.close()
        self.assertEqual(self.portal.wx.message_digests(CHAT, 5, 100), ["f" * 32])
        self.assertEqual(self.portal.wx.message_digests("other@chatroom", 5, 100), [])

    def video(self, month, name, data=b"\x00\x00\x00\x20ftypisom"):
        folder = self.account / "msg/video" / month
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    def test_video_resolves_main_and_raw_of_same_stem(self):
        stem = "1" * 32
        self.video("2026-09", stem + ".mp4")
        self.video("2026-09", stem + "_raw.mp4")
        self.hardlink("video_hardlink_info_v4", [("2" * 32, stem + ".mp4", 10, "2026-09")])
        main, raw = media.video_source(self.portal.wx, ["2" * 32])
        self.assertEqual(main.name, stem + ".mp4")
        self.assertEqual(raw.name, stem + "_raw.mp4")

    def test_video_with_two_different_files_is_ambiguous(self):
        self.video("2026-09", "3" * 32 + ".mp4")
        self.video("2026-09", "4" * 32 + ".mp4")
        self.hardlink("video_hardlink_info_v4", [("5" * 32, "3" * 32 + ".mp4", 10, "2026-09"),
                                                 ("5" * 32, "4" * 32 + ".mp4", 10, "2026-09")])
        with self.assertRaisesRegex(Failure, "MEDIA_SOURCE_AMBIGUOUS"):
            media.video_source(self.portal.wx, ["5" * 32])

    def test_video_not_downloaded_is_reported_as_not_cached(self):
        self.hardlink("video_hardlink_info_v4", [("6" * 32, "7" * 32 + ".mp4", 10, "2026-09")])
        with self.assertRaisesRegex(Failure, "MEDIA_NOT_CACHED"):
            media.video_source(self.portal.wx, ["6" * 32])

    def test_video_without_ffmpeg_falls_back_to_cover(self):
        stem = "8" * 32
        self.video("2026-09", stem + ".mp4")
        self.video("2026-09", stem + ".jpg", b"\xff\xd8\xffcover")
        with patch.object(media, "find_tool", return_value=None):
            with self.assertRaisesRegex(Failure, "TOOL_MISSING"):
                media.extract_video(self.portal.wx, [stem], self.out / "v", frames=2)
            result = media.extract_video(self.portal.wx, [stem], self.out / "v2", frames=0)
        self.assertTrue(result["cover"].endswith("v2-cover.jpg"))
        self.assertFalse(result["audio_transcribed"])

    def file(self, month, name, data=b"%PDF-1.4\n"):
        folder = self.account / "msg/file" / month
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    def test_identical_copies_prefer_original_title(self):
        self.file("2026-09", "报价.pdf")
        self.file("2026-09", "报价(1).pdf")
        self.hardlink("file_hardlink_info_v4", [("b" * 32, "报价(1).pdf", 9, "2026-09"), ("b" * 32, "报价.pdf", 9, "2026-09")])
        path, copies = media.file_source(self.portal.wx, file_row(), file_row()["content"]["File"])
        self.assertEqual((path.name, copies), ("报价.pdf", 2))

    def test_same_md5_with_different_sizes_is_ambiguous(self):
        self.file("2026-09", "报价.pdf")
        self.file("2026-09", "报价(1).pdf", b"different length")
        self.hardlink("file_hardlink_info_v4", [("b" * 32, "报价(1).pdf", 9, "2026-09"), ("b" * 32, "报价.pdf", 9, "2026-09")])
        with self.assertRaisesRegex(Failure, "MEDIA_SOURCE_AMBIGUOUS"):
            media.file_source(self.portal.wx, file_row(), file_row()["content"]["File"])

    def test_file_falls_back_to_month_name_and_size(self):
        month = dt.datetime.fromtimestamp(file_row()["create_time"]).strftime("%Y-%m")
        self.file(month, "报价.pdf")
        self.hardlink("file_hardlink_info_v4", [])
        path, _ = media.file_source(self.portal.wx, file_row(), file_row()["content"]["File"])
        self.assertEqual(path.name, "报价.pdf")

    def test_index_entries_outside_month_folders_are_ignored(self):
        self.hardlink("file_hardlink_info_v4", [("b" * 32, "报价.pdf", 9, "..")])
        with self.assertRaisesRegex(Failure, "MEDIA_NOT_CACHED"):
            media.file_source(self.portal.wx, file_row(create_time=1600000000), file_row()["content"]["File"])

    def test_file_extraction_writes_private_text(self):
        self.file("2026-09", "纪要.md", "会议结论：周五前交付".encode())
        meta = {"title": "纪要.md", "file_ext": "md", "file_size": len("会议结论：周五前交付".encode()), "md5": "c" * 32}
        self.hardlink("file_hardlink_info_v4", [("c" * 32, "纪要.md", meta["file_size"], "2026-09")])
        result = media.extract_file(self.portal.wx, file_row(), meta, self.out / "doc")
        text = Path(result["text_path"]).read_text()
        self.assertIn("会议结论：周五前交付", text)
        self.assertEqual(result["method"], "plain")
        self.assertEqual(Path(result["text_path"]).stat().st_mode & 0o777, 0o600)


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_plain_text_in_gb18030(self):
        path = self.root / "a.txt"
        path.write_bytes("预算 30 万".encode("gb18030"))
        text, info = documents.extract_text(path)
        self.assertEqual(text, "预算 30 万")
        self.assertEqual(info["method"], "plain")

    def test_zip_is_listed_not_extracted(self):
        path = self.root / "b.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("素材/1.png", b"x")
        text, info = documents.extract_text(path)
        self.assertIn("素材/1.png", text)
        self.assertEqual(info["method"], "zip-listing")

    def test_images_and_unknown_types_are_refused(self):
        for name in ("c.png", "d.exe"):
            (self.root / name).write_bytes(b"x")
            with self.assertRaisesRegex(Failure, "DOCUMENT_UNSUPPORTED"):
                documents.extract_text(self.root / name)

    def test_docx_falls_back_to_raw_ooxml(self):
        path = self.root / "e.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<w:document><w:p><w:t>交付标准</w:t></w:p></w:document>")
        with patch.object(documents, "find_tool", return_value=None):
            text, info = documents.extract_text(path)
        self.assertIn("交付标准", text)
        self.assertTrue(info["approximate"])

    def test_utf16_scan_drops_font_table_noise(self):
        noise = "V一V伀V倀V儀V刀V匀V吀V唀".encode("utf-16le")
        body = "口播：先介绍产品卖点，再展示使用场景".encode("utf-16le")
        path = self.root / "f.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 16 + noise + b"\x00\x00" * 4 + body)
        text = documents._utf16_scan(path)
        self.assertIn("先介绍产品卖点", text)
        self.assertNotIn("伀", text)


class FeishuTests(unittest.TestCase):
    def test_non_feishu_url_is_never_contacted(self):
        with patch.object(feishu, "run") as runner:
            with self.assertRaisesRegex(Failure, "LINK_NOT_FEISHU"):
                feishu.fetch("https://feishu.cn.evil.example/docx/A")
        runner.assert_not_called()

    def test_error_mapping_from_structured_errors(self):
        cases = [({"type": "auth", "message": "x"}, "FEISHU_AUTH_REQUIRED"),
                 ({"type": "api", "message": "No permission to access this document"}, "FEISHU_NO_ACCESS"),
                 ({"type": "api", "subtype": "invalid_parameters", "message": "token is invalid"}, "FEISHU_NOT_FOUND"),
                 ({"type": "api", "message": "Invalid document_id or document not found. Verify it is accessible."},
                  "FEISHU_NOT_FOUND"),
                 ({"type": "api", "message": "server busy"}, "FEISHU_FETCH_FAILED")]
        for error, expected in cases:
            self.assertEqual(feishu._error_code(error), expected, error)

    def test_wiki_sheet_reads_requested_sheet_first(self):
        calls = []

        def lark(args, timeout=90):
            calls.append(args[:2])
            if args[:2] == ["wiki", "+node-get"]:
                return {"node": {"obj_type": "sheet", "title": "执行表"}}
            if args[:2] == ["sheets", "+workbook-info"]:
                return {"title": "执行表", "sheets": [{"sheet_id": "A", "sheet_name": "总表"},
                                                   {"sheet_id": "H", "sheet_name": "隐藏", "is_hidden": True},
                                                   {"sheet_id": "S1", "sheet_name": "日表"}]}
            return {"annotated_csv": "[row=1] 日期,消耗\n[row=2] 9/1,\"1,000\"", "actual_range": "A1:B2"}

        with patch.object(feishu, "_lark", side_effect=lark):
            title, body, details = feishu.fetch("https://t.feishu.cn/wiki/AbCdEf123?sheet=S1")
        self.assertEqual(title, "执行表")
        self.assertEqual([s["sheet_id"] for s in details["sheets"]], ["S1", "A"])
        self.assertNotIn("[row=", body)
        self.assertIn('9/1,"1,000"', body)

    def test_unsupported_feishu_types(self):
        with patch.object(feishu, "_lark", return_value={"node": {"obj_type": "bitable"}}):
            with self.assertRaisesRegex(Failure, "FEISHU_UNSUPPORTED_TYPE"):
                feishu.fetch("https://t.feishu.cn/wiki/AbCdEf123")
        with self.assertRaisesRegex(Failure, "FEISHU_UNSUPPORTED_TYPE"):
            feishu.fetch("https://vc.feishu.cn/j/981971028")


class JobTests(Base):
    def test_cleanup_requires_own_job(self):
        with self.assertRaisesRegex(Failure, "CLEANUP_TARGET_INVALID"):
            jobs.cleanup_job(self.root)
        job = jobs.new_job()
        common.write_new(job / "context.json", b"{}")
        self.assertTrue(jobs.cleanup_job(job)["cleaned"])
        self.assertFalse(job.exists())

    def test_prepare_marks_partial_failure_and_no_analysis(self):
        ok = self.portal.envelope("history", [self.portal.normalize(row(msg_type=1, content={"Text": "待办"}))],
                                  {"has_more": False})
        with patch.object(self.portal, "history", side_effect=[ok, Failure("CHAT_NOT_FOUND")]):
            result = jobs.prepare(self.portal, ["one", "two"], None, None, 10)
        try:
            context = json.loads(common.private_read(result["context_path"]))
            self.assertEqual(result["failed_conversations"], 1)
            self.assertFalse(context["analysis_complete"])
            self.assertEqual(context["errors"][0]["code"], "CHAT_NOT_FOUND")
        finally:
            jobs.cleanup_job(result["job"])

    def test_prepare_collects_requested_media_and_records_failures(self):
        items = [self.portal.normalize(file_row()), self.portal.normalize(merged_row())]
        history = self.portal.envelope("history", items, {})
        with patch.object(self.portal, "history", return_value=history), \
             patch.object(self.portal, "file", side_effect=Failure("MEDIA_NOT_CACHED")), \
             patch.object(self.portal, "feishu", return_value={"status": "ready", "kind": "spreadsheet"}):
            result = jobs.prepare(self.portal, ["one"], None, None, 10, files=1, feishu_docs=1)
        try:
            context = json.loads(common.private_read(result["context_path"]))
            self.assertEqual(context["files"][0]["code"], "MEDIA_NOT_CACHED")
            self.assertEqual(result["feishu_ready"], 1)
            self.assertEqual(context["feishu"][0]["evidence_id"], items[1]["evidence_id"])
        finally:
            jobs.cleanup_job(result["job"])


if __name__ == "__main__":
    unittest.main()
