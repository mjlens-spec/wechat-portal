"""wx-cli invocation and read-only access to its decrypted cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .common import Failure, invoke

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORED_BINARY = REPO_ROOT / "vendor" / "wx-cli" / "wx-cli"
MD5_RE = re.compile(r"[0-9a-f]{32}")
# packed_info_data is protobuf; the resource md5 usually follows this field header.
PACKED_MARKER = bytes([0x12, 0x22, 0x0A, 0x20])


def packed_digests(blob):
    data = bytes(blob or b"")
    index = data.find(PACKED_MARKER)
    if index >= 0:
        candidate = data[index + 4:index + 36]
        if re.fullmatch(rb"[0-9a-fA-F]{32}", candidate):
            return [candidate.decode().lower()]
    return sorted({m.decode().lower() for m in re.findall(rb"[0-9a-fA-F]{32}", data)})


class WxCli:
    """Thin wrapper over the vendored wx-cli binary bound to one WeChat account."""

    def __init__(self, binary, account_dir, cache_dir=None):
        self.binary = str(Path(binary).expanduser()) if binary else str(VENDORED_BINARY)
        original = Path(account_dir).expanduser()
        if original.is_symlink():
            raise Failure("ACCOUNT_PATH_INVALID")
        self.account = original.resolve(strict=True)
        if not (self.account / "db_storage").is_dir():
            raise Failure("ACCOUNT_PATH_INVALID")
        self.account_tag = hashlib.sha256(str(self.account).encode()).hexdigest()[:16]
        default_cache = Path.home() / "Library/Caches/wx-cli" / self.account.name / "db_storage"
        self.cache_root = Path(cache_dir).expanduser() if cache_dir else default_cache
        self._cache_refreshed = False

    # ---- native calls -------------------------------------------------
    def query(self, args, timeout=60):
        command = [self.binary, *args, "--data-dir", str(self.account), "--format", "json", "--no-server"]
        output = invoke(command, timeout)
        try:
            value = json.loads(output)
        except ValueError:
            raise Failure("BACKEND_FORMAT_CHANGED") from None
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise Failure("BACKEND_FORMAT_CHANGED")
        return value

    def raw(self, args, timeout=60):
        try:
            return invoke([self.binary, *args], timeout)
        except Failure as exc:
            if exc.code in {"DATABASE_READ_FAILED", "BACKEND_FAILED"}:
                raise Failure("IMAGE_DECODE_FAILED") from None
            raise

    def version(self):
        return invoke([self.binary, "--version"]).strip()

    def refresh_cache(self):
        """Bring the decrypted cache up to date before reading it directly."""
        if self._cache_refreshed:
            return
        try:
            invoke([self.binary, "decrypt", "--incremental", "--data-dir", str(self.account)], timeout=300)
        except Failure:
            # A stale cache is still usable for older messages; reads below fail cleanly otherwise.
            pass
        self._cache_refreshed = True

    # ---- decrypted cache ----------------------------------------------
    def _cache_file(self, relative):
        root = self.cache_root
        if root.is_symlink() or not root.is_dir():
            raise Failure("CACHE_UNAVAILABLE")
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise Failure("CACHE_UNAVAILABLE") from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Failure("CACHE_READ_FAILED")
        if not path.resolve().is_relative_to(root.resolve()):
            raise Failure("CACHE_READ_FAILED")
        return path

    def _connect(self, relative):
        path = self._cache_file(relative)
        try:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            raise Failure("CACHE_READ_FAILED") from None

    def message_digests(self, chat, server_id, create_time):
        """Resource md5s recorded in packed_info_data for one exact message."""
        self.refresh_cache()
        table = "Msg_" + hashlib.md5(chat.encode()).hexdigest()
        folder = self.cache_root / "message"
        if not folder.is_dir():
            raise Failure("CACHE_UNAVAILABLE")
        databases = sorted(p.name for p in folder.iterdir() if re.fullmatch(r"message_\d+\.db", p.name))
        for name in databases:
            connection = self._connect(f"message/{name}")
            try:
                exists = connection.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
                if not exists:
                    continue
                row = connection.execute(
                    f'select packed_info_data from "{table}" where server_id=? and create_time=?',
                    (int(server_id), int(create_time))).fetchone()
                if row:
                    return packed_digests(row[0])
            except sqlite3.Error:
                raise Failure("CACHE_READ_FAILED") from None
            finally:
                connection.close()
        return []

    def hardlink_rows(self, kind, digests):
        """(directory, file_name, file_size, md5) rows whose md5 or file stem matches."""
        table = {"video": "video_hardlink_info_v4", "file": "file_hardlink_info_v4"}[kind]
        wanted = sorted({d.lower() for d in digests if d and MD5_RE.fullmatch(d.lower())})
        if not wanted:
            return []
        self.refresh_cache()
        connection = self._connect("hardlink/hardlink.db")
        try:
            marks = ",".join("?" * len(wanted))
            rows = connection.execute(
                f"select d.username, v.file_name, v.file_size, lower(v.md5) from {table} v "
                f"join dir2id d on d.rowid = v.dir1 where lower(v.md5) in ({marks})", wanted).fetchall()
            if kind == "video":
                # Video files are also named after a resource md5.
                names = [w + ext for w in wanted for ext in (".mp4", "_raw.mp4")]
                marks = ",".join("?" * len(names))
                rows += connection.execute(
                    f"select d.username, v.file_name, v.file_size, lower(v.md5) from {table} v "
                    f"join dir2id d on d.rowid = v.dir1 where v.file_name in ({marks})", names).fetchall()
            return sorted(set(rows))
        except sqlite3.Error:
            raise Failure("CACHE_READ_FAILED") from None
        finally:
            connection.close()
