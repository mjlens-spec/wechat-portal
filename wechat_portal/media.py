"""Locate and extract images, videos and files attached to one exact message."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import documents
from .common import Failure, copy_new, find_tool, invoke, run, write_new

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_COPY_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_COPY_BYTES = 1024 * 1024 * 1024
MAX_FRAMES = 12
MONTH_DIR = re.compile(r"\d{4}-\d{2}")


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def owned_file(path, root):
    """Accept only regular, user-owned files inside root (no symlinks)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise Failure("MEDIA_PATH_INVALID")
    if not path.resolve().is_relative_to(root.resolve()):
        raise Failure("MEDIA_PATH_INVALID")
    return True


# ---- images --------------------------------------------------------------
def image_candidates(account, chat, digests):
    attach = account / "msg" / "attach"
    folder = attach / hashlib.md5(chat.encode()).hexdigest()
    if not folder.exists():
        return []
    if folder.is_symlink() or not folder.resolve().is_relative_to(attach.resolve()):
        raise Failure("MEDIA_PATH_INVALID")
    months = sorted((p for p in folder.iterdir() if MONTH_DIR.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),
                    reverse=True)
    found = []
    for digest in digests:
        for suffix, quality in [("_h", "cached_hd"), ("", "cached_regular"), ("_t", "thumbnail")]:
            for month in months:
                path = month / "Img" / (digest + suffix + ".dat")
                if (path.exists() or path.is_symlink()) and (path, quality) not in found:
                    found.append((path, quality))
    # Quality first across digests: hd, regular, thumbnail.
    order = {"cached_hd": 0, "cached_regular": 1, "thumbnail": 2}
    return sorted(found, key=lambda item: order[item[1]])


def extract_image(wx, row, digests, stem):
    candidates = image_candidates(wx.account, row["talker"], digests)
    if not candidates:
        raise Failure("MEDIA_NOT_CACHED")
    if any(os.path.lexists(f"{stem}.{ext}") for ext in ("png", "jpg", "gif", "webp")):
        raise Failure("OUTPUT_EXISTS")
    failures = []
    for source, quality in candidates:
        try:
            owned_file(source, wx.account)
            if source.stat().st_size > MAX_IMAGE_BYTES + 4096:
                raise Failure("IMAGE_TOO_LARGE")
            with tempfile.TemporaryDirectory(prefix=".decode-", dir=Path(stem).parent) as temporary:
                raw = Path(temporary) / "decoded"
                wx.raw(["media", "decrypt-dat", str(source), "--data-dir", str(wx.account), "--output", str(raw)])
                if not raw.is_file():
                    raise Failure("IMAGE_DECODE_FAILED")
                if raw.stat().st_size > MAX_IMAGE_BYTES:
                    raise Failure("IMAGE_TOO_LARGE")
                data = raw.read_bytes()
                fmt = image_format(data)
                if not fmt:
                    raise Failure("IMAGE_FORMAT_UNSUPPORTED")
                dimensions = invoke(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(raw)])
                width = re.search(r"pixelWidth: (\d+)", dimensions)
                height = re.search(r"pixelHeight: (\d+)", dimensions)
                if not width or not height:
                    raise Failure("IMAGE_DECODE_FAILED")
                output = Path(f"{stem}.{fmt}")
                write_new(output, data)
            return {"status": "ready", "path": str(output), "format": fmt, "bytes": len(data),
                    "width": int(width[1]), "height": int(height[1]), "quality": quality,
                    "fallback_used": bool(failures), "skipped_candidate_errors": failures}
        except Failure as exc:
            if exc.code in {"OUTPUT_EXISTS", "MEDIA_PATH_INVALID", "ACCESS_DENIED"}:
                raise
            failures.append(exc.code)
    raise Failure(failures[-1] if failures else "IMAGE_DECODE_FAILED")


# ---- videos --------------------------------------------------------------
def indexed_rows(wx, kind, digests):
    """hardlink rows, or none when the index is unavailable (callers then probe the disk)."""
    try:
        return wx.hardlink_rows(kind, digests)
    except Failure as exc:
        if exc.code in ("CACHE_UNAVAILABLE", "CACHE_READ_FAILED"):
            return []
        raise


def video_source(wx, digests):
    root = wx.account / "msg" / "video"
    paths = set()
    for directory, name, _size, _md5 in indexed_rows(wx, "video", digests):
        if MONTH_DIR.fullmatch(directory) and re.fullmatch(r"[0-9a-f]{32}(_raw)?\.mp4", name):
            path = root / directory / name
            if owned_file(path, root):
                paths.add(path)
    # The index may list only one of <stem>.mp4 / <stem>_raw.mp4; pick up the sibling.
    for path in list(paths):
        base = path.name.replace("_raw.mp4", "").replace(".mp4", "")
        for sibling in (path.with_name(base + ".mp4"), path.with_name(base + "_raw.mp4")):
            if owned_file(sibling, root):
                paths.add(sibling)
    # Files are also named after a resource md5; check directly in case the index lags.
    if root.is_dir():
        for month in (p for p in root.iterdir() if MONTH_DIR.fullmatch(p.name) and p.is_dir() and not p.is_symlink()):
            for digest in digests:
                for suffix in ("", "_raw"):
                    path = month / f"{digest}{suffix}.mp4"
                    if owned_file(path, root):
                        paths.add(path)
    if not paths:
        raise Failure("MEDIA_NOT_CACHED")
    stems = {p.name.replace("_raw.mp4", "").replace(".mp4", "") for p in paths}
    if len(stems) != 1:
        raise Failure("MEDIA_SOURCE_AMBIGUOUS")
    main = sorted((p for p in paths if not p.name.endswith("_raw.mp4")), key=str) or sorted(paths, key=str)
    raw = [p for p in paths if p.name.endswith("_raw.mp4")]
    return main[0], (raw[0] if raw else None)


def probe_video(path):
    tool = find_tool("ffprobe")
    if not tool:
        return {}
    result = run([tool, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=60)
    if result.returncode:
        return {}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    return {"duration_seconds": round(duration, 2), "width": video.get("width"), "height": video.get("height"),
            "video_codec": video.get("codec_name"), "has_audio": audio is not None}


def video_frames(path, stem, count, duration):
    tool = find_tool("ffmpeg")
    if not tool:
        raise Failure("TOOL_MISSING", "ffmpeg")
    count = max(1, min(count, MAX_FRAMES))
    duration = duration or 1.0
    outputs = []
    with tempfile.TemporaryDirectory(prefix=".frames-", dir=Path(stem).parent) as folder:
        for index in range(count):
            at = duration * (index + 0.5) / count
            frame = Path(folder) / f"{index}.jpg"
            result = run([tool, "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", "-y", str(frame)], timeout=90)
            if result.returncode or not frame.is_file():
                continue
            target = Path(f"{stem}-frame-{index + 1:02d}.jpg")
            write_new(target, frame.read_bytes())
            outputs.append({"path": str(target), "at_seconds": round(at, 2)})
    return outputs


def extract_video(wx, digests, stem, frames=6, copy=False):
    main, raw = video_source(wx, digests)
    size = main.stat().st_size
    result = {"status": "ready", "source_path": str(main), "bytes": size,
              "raw_original_path": str(raw) if raw else None, "audio_transcribed": False}
    result.update(probe_video(main))
    if frames:
        result["frames"] = video_frames(main, stem, frames, result.get("duration_seconds"))
    if not result.get("frames"):
        for cover in (main.with_suffix(".jpg"), main.with_name(main.stem + "_thumb.jpg")):
            if owned_file(cover, wx.account / "msg" / "video"):
                target = Path(f"{stem}-cover.jpg")
                write_new(target, cover.read_bytes())
                result["cover"] = str(target)
                break
    if copy:
        if size > MAX_VIDEO_COPY_BYTES:
            raise Failure("VIDEO_TOO_LARGE")
        target = Path(f"{stem}.mp4")
        copy_new(main, target, MAX_VIDEO_COPY_BYTES)
        result["path"] = str(target)
    return result


# ---- files ---------------------------------------------------------------
def file_source(wx, row, meta):
    root = wx.account / "msg" / "file"
    title = meta.get("title") or ""
    size = meta.get("file_size")
    digest = (meta.get("md5") or "").lower()
    month = dt.datetime.fromtimestamp(row["create_time"]).strftime("%Y-%m")
    candidates = []
    for directory, name, _indexed_size, _md5 in indexed_rows(wx, "file", [digest]):
        if not MONTH_DIR.fullmatch(directory) or "/" in name or name in (".", ".."):
            continue
        path = root / directory / name
        if owned_file(path, root):
            candidates.append(path)
    if not candidates and title and "/" not in title and title not in (".", ".."):
        # Index can lag behind a fresh download: exact month + name + size.
        path = root / month / title
        if owned_file(path, root) and (size is None or path.stat().st_size == size):
            candidates.append(path)
    if not candidates:
        raise Failure("MEDIA_NOT_CACHED")
    sizes = {p.stat().st_size for p in candidates}
    if len(sizes) != 1:
        raise Failure("MEDIA_SOURCE_AMBIGUOUS")
    # Same md5 and size: copies such as x.pdf and x(1).pdf carry identical content.
    candidates.sort(key=lambda p: (p.name != title, p.parent.name != month, len(p.name), p.name))
    return candidates[0], len(candidates)


def extract_file(wx, row, meta, stem, text=True, copy=False, pages=None):
    source, copies = file_source(wx, row, meta)
    ext = (meta.get("file_ext") or source.suffix.lstrip(".")).lower()
    result = {"status": "ready", "name": source.name, "ext": ext, "bytes": source.stat().st_size,
              "source_path": str(source), "identical_local_copies": copies}
    if copy:
        target = Path(f"{stem}.{ext}" if ext else str(stem))
        copy_new(source, target, MAX_FILE_COPY_BYTES)
        result["path"] = str(target)
    if text:
        try:
            body, info = documents.extract_text(source, ext)
            result["text_path"] = documents.write_markdown(stem, source.name, f"微信文件 {source.name}", body, info)
            result.update({k: info[k] for k in ("method", "approximate", "chars", "truncated", "pages", "text_layer",
                                                 "visual_recommended", "chars_per_page") if k in info})
        except Failure as exc:
            result["text_error"] = exc.payload()
    if ext == "pdf":
        wanted = pages if pages is not None else (3 if result.get("visual_recommended") else 0)
        if wanted:
            try:
                result["page_images"] = documents.render_pdf_pages(source, stem, wanted, result.get("pages"))
            except Failure as exc:
                result["page_images_error"] = exc.payload()
    return result
