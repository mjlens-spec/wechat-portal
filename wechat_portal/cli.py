"""Command line for WeChat Portal. Every command prints one JSON document."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import VERSION
from .common import Failure, HINTS, private_read
from .jobs import cleanup_job, prepare
from .portal import TYPE_FILTERS, Portal

DEFAULT_CONFIG = Path.home() / ".local/share/wechat-portal/config.json"


def bounded(low, high):
    def parse(value):
        result = int(value)
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return result
    return parse


def window(parser, limit_default, limit_max=200, offset=True):
    parser.add_argument("--chat", required=True, help="会话名或精确 chat_id（建议先用 sessions 取 chat_id）")
    parser.add_argument("--since", help="YYYY-MM-DD，默认今天往前两天")
    parser.add_argument("--until", help="YYYY-MM-DD，默认今天")
    parser.add_argument("--limit", type=bounded(1, limit_max), default=limit_default)
    if offset:
        parser.add_argument("--offset", type=bounded(0, 20000), default=0)


def build_parser():
    parser = argparse.ArgumentParser(prog="wechat-portal",
                                     description="WeChat Portal：本机微信消息、图片、视频、文件与飞书链接的只读入口")
    parser.add_argument("--config", default=os.environ.get("WECHAT_PORTAL_CONFIG", str(DEFAULT_CONFIG)))
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="实际探测消息读取、媒体索引和各类工具")
    p = sub.add_parser("sessions", help="最近会话，默认过滤公众号入口")
    p.add_argument("--limit", type=bounded(1, 200), default=100)
    p.add_argument("--offset", type=bounded(0, 20000), default=0)
    p.add_argument("--include-official", action="store_true")

    p = sub.add_parser("history", help="读取消息（群聊与私聊），含链接、文件和合并转发展开")
    window(p, 100)
    p.add_argument("--type", choices=sorted(TYPE_FILTERS), default="all")

    p = sub.add_parser("media", help="列出图片 / 视频 / 文件消息及其引用")
    window(p, 20)
    p.add_argument("--kind", choices=("image", "video", "file"), required=True)
    p = sub.add_parser("images", help="同 media --kind image（兼容旧用法）")
    window(p, 20)

    p = sub.add_parser("links", help="列出消息里的链接，标注飞书文档类型")
    window(p, 200)
    p.add_argument("--feishu-only", action="store_true")

    p = sub.add_parser("image", help="按引用提取图片")
    p.add_argument("--ref", required=True)
    p.add_argument("--output", required=True, help="输出文件名前缀；所在目录须为本人私有目录（0700）")

    p = sub.add_parser("video", help="按引用读取视频：元数据、抽帧，可选复制原片")
    p.add_argument("--ref", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--frames", type=bounded(0, 12), default=6)
    p.add_argument("--copy", action="store_true", help="同时复制 MP4 到输出目录")

    p = sub.add_parser("file", help="按引用读取文件：定位本机文件、提取正文，PDF 可渲染页面")
    p.add_argument("--ref", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--no-text", action="store_true", help="不提取正文")
    p.add_argument("--copy", action="store_true", help="同时复制原文件到输出目录")
    p.add_argument("--pages", type=bounded(0, 30), help="PDF 渲染前 N 页为 PNG；缺省时仅对扫描件和幻灯片型 PDF 渲染 3 页")

    p = sub.add_parser("feishu", help="读取聊天中出现的飞书文档或表格（经 lark-cli 用户身份，只读）")
    p.add_argument("--url", required=True)
    p.add_argument("--output", required=True)

    p = sub.add_parser("prepare", help="多会话材料：消息 + 选定数量的图片、视频、文件和飞书文档")
    p.add_argument("--chat", required=True, action="append")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--limit", type=bounded(1, 200), default=100)
    p.add_argument("--images", type=bounded(0, 8), default=0)
    p.add_argument("--videos", type=bounded(0, 4), default=0)
    p.add_argument("--files", type=bounded(0, 8), default=0)
    p.add_argument("--feishu", type=bounded(0, 6), default=0)

    p = sub.add_parser("cleanup", help="只清理本工具生成的临时任务目录")
    p.add_argument("--job", required=True)
    return parser


def dispatch(args):
    if args.command == "cleanup":
        return cleanup_job(args.job)
    portal = Portal(json.loads(private_read(args.config)))
    if args.command == "status":
        return portal.status()
    if args.command == "sessions":
        return portal.sessions(args.limit, args.offset, args.include_official)
    if args.command == "history":
        return portal.history(args.chat, args.since, args.until, args.limit, args.offset, args.type)
    if args.command in ("media", "images"):
        kind = "image" if args.command == "images" else args.kind
        return portal.media_list(args.chat, kind, args.since, args.until, args.limit, args.offset)
    if args.command == "links":
        return portal.links(args.chat, args.since, args.until, args.limit, args.offset, args.feishu_only)
    if args.command == "image":
        return portal.image(args.ref, args.output)
    if args.command == "video":
        return portal.video(args.ref, args.output, args.frames, args.copy)
    if args.command == "file":
        return portal.file(args.ref, args.output, not args.no_text, args.copy, args.pages)
    if args.command == "feishu":
        return portal.feishu(args.url, args.output)
    if args.command == "prepare":
        chats = list(dict.fromkeys(args.chat))
        if len(chats) > 12:
            raise Failure("MAX_12_CHATS_PER_BATCH")
        return prepare(portal, chats, args.since, args.until, args.limit,
                       args.images, args.videos, args.files, args.feishu)
    raise Failure("LOCAL_STATE_INVALID")


def main(argv=None):
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Failure as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = "ACCESS_DENIED" if isinstance(exc, PermissionError) else "LOCAL_STATE_INVALID"
        print(json.dumps({"ok": False, "code": code, "hint": HINTS.get(code)}, ensure_ascii=False), file=sys.stderr)
        return 1
