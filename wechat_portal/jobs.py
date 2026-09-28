"""Private temporary job folders: multi-chat material for the current AI session."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from .common import Failure, now, private_dir, private_read, text_safe, write_json

JOB_PREFIX = "wechat-portal-job-"
MARKER = ".wechat-portal-job.json"


def new_job():
    root = private_dir(tempfile.mkdtemp(prefix=JOB_PREFIX))
    write_json(root / MARKER, {"tool": "wechat-portal", "owner_uid": os.getuid(), "created_at": now()})
    return root


def cleanup_job(path):
    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink():
        raise Failure("CLEANUP_TARGET_INVALID")
    root = candidate.resolve(strict=True)
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(JOB_PREFIX):
        raise Failure("CLEANUP_TARGET_INVALID")
    marker = json.loads(private_read(root / MARKER))
    if marker.get("tool") != "wechat-portal" or marker.get("owner_uid") != os.getuid():
        raise Failure("CLEANUP_TARGET_INVALID")
    shutil.rmtree(root)
    return {"cleaned": True, "job": str(root)}


def _newest(conversations, predicate):
    found = [m for c in conversations for m in c["items"] if predicate(m)]
    return sorted(found, key=lambda m: m["timestamp"], reverse=True)


def prepare(portal, chats, since, until, limit, images=0, videos=0, files=0, feishu_docs=0):
    root = new_job()
    context = {"schema_version": 2, "prepared_at": now(), "analysis_complete": False, "source": "local_wechat",
               "conversations": [], "errors": [], "images": [], "videos": [], "files": [], "feishu": []}
    try:
        for chat in chats:
            try:
                context["conversations"].append(portal.history(chat, since, until, limit))
            except Failure as exc:
                context["errors"].append({"chat_requested": text_safe(chat, 300), "code": exc.code})
        if not context["conversations"]:
            raise Failure("NO_CONVERSATION_READ")
        convs = context["conversations"]

        plan = [
            ("images", images, lambda m: (m.get("media") or {}).get("kind") == "image",
             lambda m, i: portal.image(m["media"]["ref"], root / f"image-{i:02d}")),
            ("videos", videos, lambda m: (m.get("media") or {}).get("kind") == "video",
             lambda m, i: portal.video(m["media"]["ref"], root / f"video-{i:02d}", frames=4)),
            ("files", files, lambda m: (m.get("media") or {}).get("kind") == "file",
             lambda m, i: portal.file(m["media"]["ref"], root / f"file-{i:02d}")),
        ]
        for key, wanted, predicate, action in plan:
            candidates = _newest(convs, predicate)
            for index, message in enumerate(candidates[:wanted], 1):
                try:
                    context[key].append(action(message, index))
                except Failure as exc:
                    context[key].append({"evidence_id": message["evidence_id"], "status": "unavailable",
                                         "code": exc.code, "hint": exc.payload()["hint"]})
            context[f"{key}_not_requested"] = max(0, len(candidates) - wanted)

        seen, links = set(), []
        for message in _newest(convs, lambda m: bool(m.get("links"))):
            for link in message["links"]:
                if link.get("category") == "feishu" and link.get("readable") and link["url"] not in seen:
                    seen.add(link["url"])
                    links.append((message, link))
        for index, (message, link) in enumerate(links[:feishu_docs], 1):
            try:
                result = portal.feishu(link["url"], root / f"feishu-{index:02d}")
            except Failure as exc:
                result = {"status": "unavailable", "code": exc.code, "hint": exc.payload()["hint"], "url": link["url"]}
            result["evidence_id"] = message["evidence_id"]
            context["feishu"].append(result)
        context["feishu_not_requested"] = max(0, len(links) - feishu_docs)

        write_json(root / "context.json", context)
        ready = lambda key: sum(i.get("status") == "ready" for i in context[key])
        return {"job": str(root), "context_path": str(root / "context.json"), "conversations": len(convs),
                "messages": sum(len(c["items"]) for c in convs), "failed_conversations": len(context["errors"]),
                "images_ready": ready("images"), "videos_ready": ready("videos"), "files_ready": ready("files"),
                "feishu_ready": ready("feishu"), "analysis_complete": False}
    except BaseException:
        cleanup_job(root)
        raise
