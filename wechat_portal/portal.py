"""WeChat Portal: bounded local reads of messages, media, files and linked Feishu docs."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re

from . import VERSION, feishu, media
from .backend import WxCli
from .common import Failure, HINTS, date_bounds, find_tool, now, output_stem, run, text_safe
from .documents import write_markdown
from .messages import message_kind, message_links, message_text, merged_forward

MEDIA_KINDS = {"image": 3, "video": 43, "file": 49}
# Portal --type → wx-cli --type, plus a post-filter on the normalized kind.
TYPE_FILTERS = {"all": (None, None), "text": ("text", {"text"}), "image": ("image", {"image"}),
                "video": ("video", {"video"}), "file": ("49", {"file"}), "link": ("49", {"link", "app"}),
                "merged": ("49", {"merged"}), "quote": ("49", {"quote"})}
OFFICIAL = {"brandsessionholder", "brandservicesessionholder", "notification_messages", "foldedchats"}


class Portal:
    def __init__(self, config):
        self.wx = WxCli(config.get("binary"), config["account_dir"], config.get("cache_dir"))
        self.account = self.wx.account
        self.account_tag = self.wx.account_tag

    # ---- envelope & references ------------------------------------------
    def envelope(self, kind, items, paging=None, **extra):
        return {"schema_version": 2, "tool": "wechat-portal", "version": VERSION, "kind": kind,
                "queried_at": now(), "source": "wx-cli_local_direct", "account_ref": self.account_tag,
                "items": items, "paging": paging or {}, **extra}

    def reference(self, row, kind):
        payload = {"v": 2, "account": self.account_tag, "kind": kind, "chat": row["talker"],
                   "server_id": str(row["server_id"]), "sort_seq": str(row["sort_seq"]), "timestamp": row["create_time"]}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    def unpack(self, token, kind):
        if not isinstance(token, str) or len(token) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise Failure("REFERENCE_INVALID")
        try:
            payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            if payload["v"] != 2 or payload["kind"] not in MEDIA_KINDS or not isinstance(payload["account"], str):
                raise ValueError()
            if not isinstance(payload["chat"], str) or not 1 <= len(payload["chat"]) <= 256:
                raise ValueError()
            for key in ("server_id", "sort_seq"):
                if not re.fullmatch(r"\d{1,20}", payload[key]):
                    raise ValueError()
            if not isinstance(payload["timestamp"], int) or payload["timestamp"] <= 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise Failure("REFERENCE_INVALID") from None
        if payload["account"] != self.account_tag:
            raise Failure("ACCOUNT_MISMATCH")
        if payload["kind"] != kind:
            raise Failure("REFERENCE_KIND_MISMATCH")
        return payload

    def evidence_id(self, row):
        key = f"{self.account_tag}|{row['talker']}|{row['server_id']}|{row['sort_seq']}|{row['create_time']}"
        return "wx_" + hashlib.sha256(key.encode()).hexdigest()[:20]

    # ---- normalization ---------------------------------------------------
    def normalize(self, row):
        timestamp = row["create_time"]
        kind = message_kind(row)
        result = {"evidence_id": self.evidence_id(row), "chat_id": row["talker"],
                  "message_id": str(row["server_id"]), "sort_seq": str(row["sort_seq"]),
                  "timestamp": timestamp, "time": dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                  "sender": text_safe(row.get("sender_display_name") or row.get("sender"), 300),
                  "direction": row.get("direction"), "type": kind, "text": message_text(row)}
        if kind in ("image", "video"):
            result["media"] = {"kind": kind, "ref": self.reference(row, kind)}
        elif kind == "file":
            meta = (row.get("content") or {}).get("File") or {}
            result["media"] = {"kind": "file", "ref": self.reference(row, "file"),
                               "name": text_safe(meta.get("title"), 300),
                               "ext": text_safe(meta.get("file_ext"), 20).lower(), "size": meta.get("file_size")}
        elif kind == "merged":
            record = merged_forward(row)
            if record:
                if not record.get("title"):
                    card = (row.get("content") or {}).get("MergedMessages") or {}
                    record["title"] = text_safe(card.get("title"), 200)
                result["forwarded"] = record
        links = message_links(row)
        if kind == "merged" and result.get("forwarded"):
            for item in result["forwarded"]["items"]:
                links += item.get("links", [])
        if links:
            seen, unique = set(), []
            for link in links:
                if link["url"] not in seen:
                    seen.add(link["url"])
                    unique.append(link)
            result["links"] = unique
        return result

    # ---- listing ---------------------------------------------------------
    def sessions(self, limit=100, offset=0, include_official=False):
        data = self.wx.query(["sessions", "--limit", str(limit), "--offset", str(offset)])
        items = []
        for row in data["items"]:
            chat = row["username"]
            if not include_official and (chat.startswith("gh_") or chat in OFFICIAL):
                continue
            items.append({"chat_id": chat, "name": text_safe(row.get("display_name") or chat, 300),
                          "is_group": chat.endswith("@chatroom"), "timestamp": row.get("sort_timestamp"),
                          "summary": text_safe(row.get("summary"), 500)})
        return self.envelope("sessions", items, data.get("paging"), filtered_out=len(data["items"]) - len(items),
                             next_offset=offset + len(data["items"]))

    def history(self, chat, since=None, until=None, limit=100, offset=0, type_filter="all"):
        if not chat or chat.startswith("-"):
            raise Failure("CHAT_INVALID")
        native_type, keep = TYPE_FILTERS[type_filter]
        start, end, window = date_bounds(since, until)
        args = ["query", chat, "--since", str(start), "--until", str(end), "--limit", str(limit),
                "--offset", str(offset), "--order", "desc"]
        if native_type:
            args += ["--type", native_type]
        data = self.wx.query(args)
        rows = data["items"]
        if len({r["talker"] for r in rows}) > 1:
            raise Failure("CHAT_AMBIGUOUS")
        normalized = [self.normalize(r) for r in rows]
        if keep:
            normalized = [m for m in normalized if m["type"] in keep]
        normalized.sort(key=lambda m: (m["timestamp"], int(m["sort_seq"])))
        return self.envelope("history", normalized, data.get("paging"), window=window, type_filter=type_filter,
                             chat_requested=text_safe(chat, 300), next_offset=offset + len(rows),
                             latest_message_at=max((m["time"] for m in normalized), default=None),
                             coverage="local_records_in_requested_window",
                             shard_warning_count=len(data.get("shard_warnings", [])))

    def media_list(self, chat, kind, since=None, until=None, limit=20, offset=0):
        result = self.history(chat, since, until, limit, offset, kind)
        result["kind"] = "media"
        result["media_kind"] = kind
        return result

    def links(self, chat, since=None, until=None, limit=200, offset=0, feishu_only=False):
        result = self.history(chat, since, until, limit, offset)
        found = {}
        for message in result["items"]:
            for link in message.get("links", []):
                if feishu_only and link.get("category") != "feishu":
                    continue
                entry = found.setdefault(link["url"], {**link, "mentions": []})
                entry["mentions"].append({"evidence_id": message["evidence_id"], "time": message["time"],
                                          "sender": message["sender"]})
        items = sorted(found.values(), key=lambda x: x["mentions"][-1]["time"], reverse=True)
        return self.envelope("links", items, result["paging"], window=result["window"],
                             chat_requested=result["chat_requested"], next_offset=result["next_offset"],
                             messages_scanned=len(result["items"]))

    # ---- exact message resolution -----------------------------------------
    def resolve(self, token, kind):
        ref = self.unpack(token, kind)
        anchor = ["--around-server-id", ref["server_id"]] if int(ref["server_id"]) else ["--around-sort-seq", ref["sort_seq"]]
        data = self.wx.query(["query", ref["chat"], *anchor, "--context", "0", "--limit", "1"])
        rows = [r for r in data["items"]
                if r["talker"] == ref["chat"] and str(r["server_id"]) == ref["server_id"]
                and str(r["sort_seq"]) == ref["sort_seq"] and r["create_time"] == ref["timestamp"]
                and r["msg_type"] == MEDIA_KINDS[kind] and message_kind(r) == kind]
        if len(rows) != 1:
            raise Failure("REFERENCE_NOT_FOUND")
        return rows[0]

    def _digests(self, row, kind):
        key = {"image": "Image", "video": "Video", "file": "File"}[kind]
        declared = ((row.get("content") or {}).get(key) or {}).get("md5")
        digests = [declared.lower()] if isinstance(declared, str) and re.fullmatch(r"[0-9a-fA-F]{32}", declared) else []
        if kind != "file":
            try:
                digests += [d for d in self.wx.message_digests(row["talker"], row["server_id"], row["create_time"])
                            if d not in digests]
            except Failure:
                if not digests:
                    raise
        if not digests:
            raise Failure("MEDIA_METADATA_MISSING")
        return digests

    def _wrap(self, row, result):
        result["evidence_id"] = self.evidence_id(row)
        result["chat_id"] = row["talker"]
        result["time"] = dt.datetime.fromtimestamp(row["create_time"]).astimezone().isoformat()
        return result

    def image(self, token, output):
        row = self.resolve(token, "image")
        stem = output_stem(output)
        return self._wrap(row, media.extract_image(self.wx, row, self._digests(row, "image"), stem))

    def video(self, token, output, frames=6, copy=False):
        row = self.resolve(token, "video")
        stem = output_stem(output)
        return self._wrap(row, media.extract_video(self.wx, self._digests(row, "video"), stem, frames, copy))

    def file(self, token, output, text=True, copy=False, pages=None):
        row = self.resolve(token, "file")
        meta = (row.get("content") or {}).get("File") or {}
        if not meta.get("md5") and not meta.get("title"):
            raise Failure("MEDIA_METADATA_MISSING")
        stem = output_stem(output)
        return self._wrap(row, media.extract_file(self.wx, row, meta, stem, text, copy, pages))

    def feishu(self, url, output):
        stem = output_stem(output)
        title, body, details = feishu.fetch(url)
        info = {"method": "lark-cli", "chars": len(body), "truncated": False}
        path = write_markdown(stem, title or details.get("feishu_type") or "飞书文档", url, body, info)
        return {"status": "ready", "title": title, "text_path": path, "chars": len(body), **details}

    # ---- status ----------------------------------------------------------
    def status(self):
        result = {"tool": "wechat-portal", "version": VERSION, "backend": text_safe(self.wx.version(), 200),
                  "backend_path": self.wx.binary, "dashboard_required": False, "model_api_used": False,
                  "account_ref": self.account_tag, "messages": "unchecked", "checked_at": now()}
        try:
            self.wx.query(["sessions", "--limit", "1"])
            result["messages"] = "ready"
        except Failure as exc:
            result["messages"] = "blocked"
            result["message_error"] = {"code": exc.code, "hint": HINTS.get(exc.code)}
        self.wx.refresh_cache()
        result["media_index"] = "ready" if (self.wx.cache_root / "hardlink" / "hardlink.db").is_file() else "unavailable"
        tools = {name: bool(find_tool(name)) for name in
                 ("sips", "ffprobe", "ffmpeg", "pdftotext", "pdfinfo", "pdftoppm", "markitdown", "textutil", "lark-cli")}
        result["capabilities"] = {
            "images": "ready" if tools["sips"] else "missing sips",
            "videos": "frames+metadata" if tools["ffmpeg"] and tools["ffprobe"] else "source path only (install ffmpeg)",
            "pdf": "ready" if tools["pdftotext"] else "limited (install poppler)",
            "office": "ready" if tools["markitdown"] else ("docx/doc via textutil" if tools["textutil"] else "limited"),
            "feishu": self._feishu_status() if tools["lark-cli"] else "lark-cli missing",
            "voice": "not supported",
        }
        result["tools"] = tools
        return result

    def _feishu_status(self):
        lark = find_tool("lark-cli")
        probe = run([lark, "auth", "status"], timeout=20)
        try:
            user = json.loads(probe.stdout or "{}").get("identities", {}).get("user", {})
        except ValueError:
            return "unknown"
        return "ready" if user.get("available") else "login required (lark-cli auth login)"
