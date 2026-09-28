"""Read Feishu / Lark documents linked in chats, through the user's lark-cli login.

Only Feishu hosts are ever contacted; URLs come from chat content and are untrusted.
All calls are read-only (`--as user`).
"""
from __future__ import annotations

import json
import re

from .common import Failure, find_tool, run, text_safe
from .messages import classify_link

MAX_SHEETS = 8
MAX_SHEET_CHARS = 80_000
MAX_TOTAL_CHARS = 400_000
ROW_PREFIX = re.compile(r"^\[row=\d+\] ", re.M)


def _lark(args, timeout=90):
    tool = find_tool("lark-cli")
    if not tool:
        raise Failure("FEISHU_CLI_MISSING")
    result = run([tool, *args, "--as", "user"], timeout=timeout)
    value = _json(result.stdout)
    if result.returncode == 0 and value.get("ok"):
        return value.get("data") or {}
    # lark-cli writes the error envelope to stderr.
    error = value.get("error") or _json(result.stderr).get("error") or {}
    raise Failure(_error_code(error))


def _json(text):
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(error):
    kind = str(error.get("type") or "").lower()
    subtype = str(error.get("subtype") or "").lower()
    message = str(error.get("message") or "").lower()
    if kind == "auth" or "auth" in subtype or any(s in message for s in ("not logged in", "login required",
                                                                          "token expired", "refresh token")):
        return "FEISHU_AUTH_REQUIRED"
    if "permission" in subtype or any(s in message for s in ("permission", "forbidden", "no access",
                                                              "not have access", "无权限", "没有权限")):
        return "FEISHU_NO_ACCESS"
    if subtype == "invalid_parameters" or any(s in message for s in ("not found", "not exist", "deleted",
                                                                      "token is invalid", "invalid document", "不存在")):
        return "FEISHU_NOT_FOUND"
    return "FEISHU_FETCH_FAILED"


def _doc(url):
    data = _lark(["docs", "+fetch", "--doc", url, "--doc-format", "markdown"], timeout=120)
    content = (data.get("document") or {}).get("content")
    if not isinstance(content, str):
        raise Failure("FEISHU_FETCH_FAILED")
    title = re.search(r"<title>(.*?)</title>", content)
    body = re.sub(r"^\s*<title>.*?</title>\s*", "", content, count=1, flags=re.S)
    return (title[1].strip() if title else ""), body


def _sheet(url, wanted=None):
    info = _lark(["sheets", "+workbook-info", "--url", url])
    sheets = [s for s in info.get("sheets") or [] if isinstance(s, dict) and s.get("sheet_id")]
    if wanted:
        sheets.sort(key=lambda s: s["sheet_id"] != wanted)
    chosen = [s for s in sheets if s["sheet_id"] == wanted or not s.get("is_hidden")][:MAX_SHEETS]
    sections, summary, total = [], [], 0
    for sheet in chosen:
        data = _lark(["sheets", "+csv-get", "--url", url, "--sheet-id", sheet["sheet_id"]], timeout=120)
        csv_text = ROW_PREFIX.sub("", data.get("annotated_csv") or "")
        clipped = csv_text[:MAX_SHEET_CHARS]
        truncated = len(csv_text) > MAX_SHEET_CHARS or bool(data.get("has_more"))
        if total + len(clipped) > MAX_TOTAL_CHARS:
            break
        total += len(clipped)
        name = text_safe(sheet.get("sheet_name") or sheet["sheet_id"], 200)
        sections.append(f"## {name}\n\n范围 {data.get('actual_range', '')}{'（已截断）' if truncated else ''}\n\n```csv\n{clipped}\n```")
        summary.append({"sheet_id": sheet["sheet_id"], "name": name, "range": data.get("actual_range"),
                        "truncated": truncated})
    skipped = len(sheets) - len(summary)
    return text_safe(info.get("title") or "", 300), sections, summary, skipped


def fetch(url):
    """Return (title, markdown_body, details). Raises Failure for anything unreadable."""
    link = classify_link(url)
    if link.get("category") != "feishu":
        raise Failure("LINK_NOT_FEISHU")
    kind = link.get("feishu_type")
    title = ""
    details = {"url": url, "feishu_type": kind}
    if kind == "wiki":
        node = _lark(["wiki", "+node-get", "--node-token", url])
        node = node.get("node") or node
        kind = {"docx": "docx", "doc": "docx", "sheet": "sheets"}.get(node.get("obj_type"), node.get("obj_type") or "unknown")
        title = text_safe(node.get("title") or "", 300)
        details["wiki_obj_type"] = node.get("obj_type")
    if kind in ("docx", "docs"):
        doc_title, body = _doc(url)
        details["kind"] = "document"
        return (doc_title or title), body, details
    if kind == "sheets":
        book_title, sections, sheets, skipped = _sheet(url, link.get("sheet_id"))
        details.update({"kind": "spreadsheet", "sheets": sheets, "sheets_skipped": skipped})
        return (title or book_title), "\n\n".join(sections), details
    raise Failure("FEISHU_UNSUPPORTED_TYPE", kind)
