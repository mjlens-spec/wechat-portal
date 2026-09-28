"""Shared primitives: failures, redaction, private file IO, subprocess calls."""
from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

MAX_TEXT = 16000

HINTS = {
    "ACCESS_DENIED": "请允许宿主访问微信本地数据和读取器缓存；这不等于密钥失效。",
    "DATABASE_READ_FAILED": "数据库读取失败；核对当前账号、文件权限和该数据库密钥。",
    "KEY_UNAVAILABLE": "当前读取路径缺少可用密钥；先保留现有配置并定位具体数据库。",
    "MEDIA_NOT_CACHED": "本机没有该消息的图片、视频或文件；在微信里点开并完成下载后重试。",
    "MEDIA_METADATA_MISSING": "消息中缺少媒体定位信息，无法安全关联本地文件。",
    "MEDIA_PATH_INVALID": "媒体文件路径不符合安全要求（符号链接、属主不符或越出账号目录）。",
    "MEDIA_SOURCE_AMBIGUOUS": "同一条消息匹配到多个内容不同的本地文件，已停止以免关联错误。",
    "IMAGE_FORMAT_UNSUPPORTED": "图片已定位，但当前工具无法转换为可查看格式。",
    "IMAGE_DECODE_FAILED": "图片解码失败；可换一个清晰度缓存或在微信中重新打开图片。",
    "IMAGE_TOO_LARGE": "图片超过本工具的 12 MB 上限。",
    "VIDEO_TOO_LARGE": "视频超过复制上限；可不带 --copy，只取抽帧和元数据。",
    "FILE_TOO_LARGE": "文件超过本工具的大小上限。",
    "ACCOUNT_MISMATCH": "引用与当前绑定的微信账号不一致，请重新列出消息。",
    "REFERENCE_INVALID": "引用格式无效，请从 history 或 media 的返回中复制完整引用。",
    "REFERENCE_NOT_FOUND": "未找到完全一致的消息，请刷新列表后重试。",
    "REFERENCE_KIND_MISMATCH": "引用类型与命令不一致：图片用 image，视频用 video，文件用 file。",
    "OUTPUT_EXISTS": "目标文件已存在，请使用新的文件名。",
    "CHAT_INVALID": "会话参数无效。",
    "CHAT_AMBIGUOUS": "会话名称匹配多个结果，请从 sessions 中选择准确的 chat_id。",
    "CHAT_NOT_FOUND": "找不到该会话；用 sessions 查看准确的会话名或 chat_id。",
    "BACKEND_NOT_INSTALLED": "找不到配置的 wx-cli 程序，请核对安装位置。",
    "BACKEND_TIMEOUT": "本次读取超时；可缩小会话或日期范围后重试。",
    "BACKEND_FORMAT_CHANGED": "wx-cli 返回格式不兼容，请核对内置程序版本。",
    "BACKEND_FAILED": "wx-cli 读取失败。",
    "CACHE_UNAVAILABLE": "找不到 wx-cli 解密缓存；先运行 status 或任意读取命令。",
    "CACHE_READ_FAILED": "读取 wx-cli 解密缓存失败；稍后重试，或运行 status 刷新缓存。",
    "PRIVATE_FILE_REQUIRED": "配置和临时材料须属于当前用户，文件权限设为 0600。",
    "PRIVATE_DIRECTORY_REQUIRED": "输出目录须属于当前用户，目录权限设为 0700。",
    "SYMLINK_REFUSED": "拒绝写入符号链接路径。",
    "TOOL_MISSING": "缺少本机工具，见 detail 字段。",
    "DOCUMENT_UNSUPPORTED": "暂不支持该文件类型的正文提取；可直接用宿主工具打开 source_path。",
    "DOCUMENT_EXTRACT_FAILED": "文档正文提取失败；可直接用宿主工具打开 source_path。",
    "LINK_NOT_FEISHU": "只读取飞书 / Lark 文档链接，其他网址不会被访问。",
    "FEISHU_CLI_MISSING": "找不到 lark-cli，无法读取飞书文档。",
    "FEISHU_AUTH_REQUIRED": "lark-cli 用户身份未登录或授权过期，请运行 lark-cli auth login。",
    "FEISHU_NO_ACCESS": "当前飞书账号没有该文档的查看权限（常见于其他企业租户的文档）。",
    "FEISHU_NOT_FOUND": "飞书文档不存在、已删除或链接无效。",
    "FEISHU_UNSUPPORTED_TYPE": "该飞书链接类型暂不支持读取正文（如多维表格、妙记、会议、表单）。",
    "FEISHU_FETCH_FAILED": "飞书文档读取失败。",
    "INVALID_DATE": "日期格式应为 YYYY-MM-DD。",
    "INVALID_DATE_RANGE": "起始日期晚于结束日期。",
    "CLEANUP_TARGET_INVALID": "只能清理本工具生成的临时任务目录。",
    "NO_CONVERSATION_READ": "所有会话都读取失败，未生成材料。",
    "MAX_12_CHATS_PER_BATCH": "每批最多 12 个会话。",
    "ACCOUNT_PATH_INVALID": "账号目录无效，须直接包含 db_storage 且不是符号链接。",
    "LOCAL_STATE_INVALID": "本机配置或状态文件无效。",
}


class Failure(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code)

    def payload(self):
        value = {"ok": False, "code": self.code, "hint": HINTS.get(self.code)}
        if self.detail:
            value["detail"] = self.detail
        return value


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def text_safe(value, limit=MAX_TEXT):
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<\?xml.*|<msg(?:\s|>).*", "[图片或消息底层元数据已隐藏]", text)
    text = re.sub(
        r"(?i)\b(aeskey|authkey|access_token|refresh_token|token|password|passwd|secret|enc_key|data_key|image_aes_key)\b"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'<>;&,}]+)",
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]+", "Bearer [已隐藏]", text)
    return text[:limit]


def private_read(path, max_bytes=4 * 1024 * 1024):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Failure("PRIVATE_FILE_REQUIRED")
        if info.st_size > max_bytes:
            raise Failure("FILE_TOO_LARGE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(max_bytes + 1)
    finally:
        os.close(fd)


def private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise Failure("SYMLINK_REFUSED")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Canonicalize system aliases such as /tmp -> /private/tmp.
    path = path.resolve(strict=True)
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure("PRIVATE_DIRECTORY_REQUIRED")
    return path


def output_stem(value):
    """Resolve an output prefix whose parent must be a private directory."""
    stem = Path(value).expanduser().absolute()
    return private_dir(stem.parent) / stem.name


def write_new(path, data):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Failure("OUTPUT_EXISTS") from None
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
        os.fchmod(fd, 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def copy_new(source, target, max_bytes):
    """Stream a file into a new private file without following links."""
    target = Path(target)
    if Path(source).stat().st_size > max_bytes:
        raise Failure("FILE_TOO_LARGE")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Failure("OUTPUT_EXISTS") from None
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb", closefd=False) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.fchmod(fd, 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def write_json(path, value):
    write_new(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def date_bounds(since=None, until=None):
    today = dt.date.today()
    try:
        first = dt.date.fromisoformat(since) if since else today - dt.timedelta(days=2)
        last = dt.date.fromisoformat(until) if until else today
    except ValueError:
        raise Failure("INVALID_DATE") from None
    if first > last:
        raise Failure("INVALID_DATE_RANGE")
    start = int(dt.datetime.combine(first, dt.time.min).timestamp())
    # wx-cli uses an inclusive upper bound.
    end = int(dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min).timestamp()) - 1
    window = {"since": first.isoformat(), "until": last.isoformat(),
              "timezone": str(dt.datetime.now().astimezone().tzinfo)}
    return start, end, window


TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), "/usr/bin", "/bin", "/usr/sbin")


def tool_env():
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join(dict.fromkeys(parts + list(TOOL_DIRS)))
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    return env


def find_tool(name):
    return shutil.which(name, path=tool_env()["PATH"])


def require_tool(name):
    path = find_tool(name)
    if not path:
        raise Failure("TOOL_MISSING", name)
    return path


def run(argv, timeout=60, text=True):
    """Run a local program; never forward its output to the caller unfiltered."""
    try:
        return subprocess.run(argv, capture_output=True, text=text, timeout=timeout,
                              umask=0o077, env=tool_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Failure("BACKEND_TIMEOUT") from None
    except PermissionError:
        raise Failure("ACCESS_DENIED") from None
    except FileNotFoundError:
        raise Failure("BACKEND_NOT_INSTALLED") from None


# wx-cli prints these on every call (e.g. DBs without a stored key); they are not the failure cause.
NOISE_LINE = re.compile(r"^\s*(decrypt err\b|cache:|note:|auto-detected account)", re.I)


def error_code(stderr):
    lines = [line for line in (stderr or "").lower().splitlines() if not NOISE_LINE.match(line)]
    value = "\n".join(lines)
    if any(s in value for s in ["permission denied", "operation not permitted", "readonly database", "unable to open database file"]):
        return "ACCESS_DENIED"
    if "ambiguous" in value or "multiple contacts" in value:
        return "CHAT_AMBIGUOUS"
    if re.search(r"(contact|chat|session)\b.*\bnot found", value):
        return "CHAT_NOT_FOUND"
    if "key" in value and any(s in value for s in ["not found", "no key", "missing", "unavailable", "derivation"]):
        return "KEY_UNAVAILABLE"
    if "decrypt" in value or "database" in value:
        return "DATABASE_READ_FAILED"
    return "BACKEND_FAILED"


def invoke(argv, timeout=60):
    result = run(argv, timeout)
    if result.returncode:
        # Older wx-cli versions print key previews on failure. Never forward either stream.
        raise Failure(error_code(result.stderr))
    return result.stdout
