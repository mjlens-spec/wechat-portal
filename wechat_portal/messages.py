"""Message text, link extraction and merged-forward (聊天记录) expansion."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from .common import text_safe

TYPE_NAMES = {1: "text", 3: "image", 34: "voice", 42: "card", 43: "video", 47: "emoji", 48: "location",
              49: "app", 50: "call", 10000: "system", 10002: "revoke"}
APP_TYPES = {"File": "file", "Link": "link", "Quote": "quote", "MergedMessages": "merged",
             "MiniProgram": "miniprogram", "ChannelVideo": "channel_video", "AppGeneric": "app", "Pat": "pat"}
# Forwarded record item types (recordinfo/datalist/dataitem@datatype).
RECORD_TYPES = {"1": "text", "2": "image", "3": "voice", "4": "video", "5": "link", "6": "location",
                "8": "file", "17": "merged", "19": "miniprogram", "22": "channel_video"}

URL_RE = re.compile(r"https?://[^\s<>\"'　-〿一-鿿＀-￯]+")
TRAILING = ".,;:!?)]}>'\""
FEISHU_HOSTS = ("feishu.cn", "larksuite.com", "larkoffice.com", "feishu.net")
FEISHU_READABLE = {"docx", "docs", "wiki", "sheets"}
MAX_RAW_XML = 2 * 1024 * 1024
MAX_RECORD_ITEMS = 300


def message_kind(row):
    content = row.get("content") or {}
    if row.get("msg_type") == 49 and isinstance(content, dict):
        for key in content:
            if key in APP_TYPES:
                return APP_TYPES[key]
    return TYPE_NAMES.get(row.get("msg_type"), "unsupported")


def _app(row, key):
    content = row.get("content") or {}
    value = content.get(key) if isinstance(content, dict) else None
    return value if isinstance(value, dict) else {}


def human_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def message_text(row):
    content = row.get("content") or {}
    if isinstance(content, dict):
        for kind, data in content.items():
            if kind in ("Text", "System", "Revoke") and isinstance(data, str):
                return text_safe(data)
            if kind == "Quote" and isinstance(data, dict):
                return text_safe((data.get("reply_text") or "") + "\n引用 " + (data.get("refer_sender") or "")
                                 + "：" + (data.get("refer_content") or ""))
            if kind == "File" and isinstance(data, dict):
                size = human_size(data.get("file_size"))
                return text_safe("[文件] " + (data.get("title") or "") + (f"（{size}）" if size else ""))
            if kind == "MergedMessages" and isinstance(data, dict):
                return text_safe("[聊天记录] " + (data.get("title") or ""))
            if kind in ("Link", "MiniProgram", "AppGeneric", "ChannelVideo") and isinstance(data, dict):
                return text_safe("\n".join(str(data[k]) for k in ("title", "des", "url") if data.get(k)))
            if kind == "Video":
                return "[视频]"
            if kind == "Image":
                return "[图片]"
    return text_safe(row.get("snippet") or "[" + TYPE_NAMES.get(row.get("msg_type"), "unsupported") + "]")


# ---- links -------------------------------------------------------------
def clean_url(url):
    url = html.unescape(url)
    while url and url[-1] in TRAILING:
        url = url[:-1]
    return url


def extract_urls(*texts):
    found = []
    for text in texts:
        for match in URL_RE.findall(html.unescape(str(text or ""))):
            url = clean_url(match)
            if len(url) > 12 and url not in found:
                found.append(url)
    return found


def classify_link(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    info = {"url": url, "host": host}
    if any(host == h or host.endswith("." + h) for h in FEISHU_HOSTS):
        kind = segments[0] if segments else ""
        info.update({"category": "feishu", "feishu_type": kind or "unknown",
                     "readable": kind in FEISHU_READABLE and len(segments) >= 2})
        if kind in FEISHU_READABLE and len(segments) >= 2:
            info["token"] = segments[1]
        sheet = parse_qs(parts.query).get("sheet")
        if sheet:
            info["sheet_id"] = sheet[0]
        if host.startswith("vc.") or kind == "j":
            info["feishu_type"] = "meeting"
            info["readable"] = False
        return info
    if host.endswith("xiaohongshu.com") or host == "xhslink.com":
        info["category"] = "xiaohongshu"
    elif host == "mp.weixin.qq.com":
        info["category"] = "wechat_article"
    elif host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        info["category"] = "douyin"
    else:
        info["category"] = "web"
    return info


def message_links(row):
    content = row.get("content") or {}
    texts = []
    if isinstance(content, dict):
        for kind, data in content.items():
            if isinstance(data, str) and kind in ("Text", "System"):
                texts.append(data)
            elif isinstance(data, dict):
                texts += [data.get(k) for k in ("url", "des", "title", "reply_text", "refer_content") if data.get(k)]
    return [classify_link(u) for u in extract_urls(*texts)]


# ---- merged forward ------------------------------------------------------
def _child_text(node, tag):
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_xml(text):
    if not text or len(text) > MAX_RAW_XML or "<!DOCTYPE" in text or "<!ENTITY" in text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def parse_record(record_xml, depth=0):
    """Expand a <recordinfo> document into readable items."""
    root = _parse_xml(record_xml)
    if root is None:
        return None
    if root.tag != "recordinfo":
        root = root.find(".//recordinfo")
        if root is None:
            return None
    items = []
    datalist = root.find("datalist")
    nodes = list(datalist.findall("dataitem")) if datalist is not None else []
    for node in nodes[:MAX_RECORD_ITEMS]:
        kind = RECORD_TYPES.get(node.get("datatype", ""), "other")
        text = _child_text(node, "datadesc")
        title = _child_text(node, "datatitle")
        item = {"sender": text_safe(_child_text(node, "sourcename"), 200),
                "time": _child_text(node, "sourcetime"), "type": kind}
        if kind == "text":
            item["text"] = text_safe(text)
        elif kind == "file":
            size = human_size(_child_text(node, "datasize"))
            item["text"] = text_safe("[文件] " + (title or text) + (f"（{size}）" if size else ""))
        elif kind == "link":
            url = _child_text(node, "link") or _child_text(node, "weburl")
            item["text"] = text_safe("\n".join(x for x in (title, text, url) if x))
        elif kind == "merged":
            nested = _child_text(node, "recordxml")
            inner = node.find("recordxml")
            nested_xml = ET.tostring(inner.find("recordinfo"), encoding="unicode") if inner is not None and inner.find("recordinfo") is not None else nested
            sub = parse_record(nested_xml, depth + 1) if depth < 2 and nested_xml else None
            item["text"] = text_safe("[聊天记录] " + (title or text))
            if sub:
                item["forwarded"] = sub
        else:
            label = {"image": "[图片]", "video": "[视频]", "voice": "[语音]", "location": "[位置]",
                     "miniprogram": "[小程序]", "channel_video": "[视频号]"}.get(kind, "[其他]")
            item["text"] = text_safe((label + " " + (title or text)).strip())
        links = [classify_link(u) for u in extract_urls(text, title, item.get("text"))]
        if links:
            item["links"] = links
        items.append(item)
    return {"title": text_safe(_child_text(root, "title"), 200), "count": len(nodes),
            "items": items, "truncated": len(nodes) > MAX_RECORD_ITEMS}


def merged_forward(row):
    raw = _app(row, "MergedMessages").get("raw_xml")
    if not isinstance(raw, str):
        return None
    root = _parse_xml(raw)
    if root is None:
        return None
    record = root.find(".//recorditem")
    if record is None or not (record.text or "").strip():
        return None
    return parse_record(record.text.strip())
