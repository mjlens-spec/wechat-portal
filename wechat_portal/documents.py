"""Document → text for files received in WeChat. Uses local tools only."""
from __future__ import annotations

from pathlib import Path
import re
import tempfile
import zipfile

from .common import Failure, find_tool, run, write_new

MAX_DOC_CHARS = 400_000
PDF_TEXT_LAYER_MIN = 20     # non-space chars per page below which the PDF is treated as scanned
PDF_SPARSE_TEXT = 200       # below this, page images are worth looking at (slides, charts)
MARKITDOWN_TYPES = {"docx", "pptx", "xlsx", "xlsm", "xls", "html", "htm", "csv", "epub"}
TEXTUTIL_TYPES = {"doc", "docx", "rtf", "rtfd", "odt", "webarchive", "wordml"}
SOFFICE_TEXT_TYPES = {"doc", "docx", "rtf", "odt", "wps"}      # LibreOffice → plain text
SOFFICE_PDF_TYPES = {"ppt", "pps", "odp"}                      # LibreOffice → PDF → pdftotext
PLAIN_TYPES = {"txt", "md", "markdown", "json", "log", "tsv", "xml", "yaml", "yml"}
IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "heic", "tif", "tiff", "bmp"}
SUPPORTED = MARKITDOWN_TYPES | TEXTUTIL_TYPES | SOFFICE_TEXT_TYPES | SOFFICE_PDF_TYPES | PLAIN_TYPES | {"pdf", "zip"}
UTF16_RUN = re.compile(rb"(?:[\x20-\x7e\x09\x0a\x0d]\x00|[\x00-\xff][\x4e-\x9f]|[\x00-\xff][\x30\xff]){8,}")


def _decode(data):
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(path):
    tool = find_tool("pdftotext")
    info = {}
    pdfinfo = find_tool("pdfinfo")
    if pdfinfo:
        meta = run([pdfinfo, str(path)], timeout=30)
        pages = re.search(r"^Pages:\s+(\d+)", meta.stdout or "", re.M)
        if pages:
            info["pages"] = int(pages[1])
    if tool:
        result = run([tool, "-layout", "-enc", "UTF-8", str(path), "-"], timeout=180)
        if result.returncode == 0:
            text = result.stdout or ""
            info["method"] = "pdftotext"
            per_page = len(re.sub(r"\s+", "", text)) / max(1, info.get("pages", 1))
            info["chars_per_page"] = round(per_page)
            # Scans have no text layer; slide-style PDFs carry little text. Both need a visual look.
            info["text_layer"] = per_page >= PDF_TEXT_LAYER_MIN
            info["visual_recommended"] = per_page < PDF_SPARSE_TEXT
            return text, info
    markitdown = find_tool("markitdown")
    if markitdown:
        result = run([markitdown, str(path)], timeout=240)
        if result.returncode == 0:
            info["method"] = "markitdown"
            info["text_layer"] = bool(result.stdout.strip())
            return result.stdout, info
    raise Failure("TOOL_MISSING", "pdftotext")


def _markitdown(path):
    tool = find_tool("markitdown")
    if not tool:
        return None
    result = run([tool, str(path)], timeout=240)
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def _textutil(path):
    tool = find_tool("textutil")
    if not tool:
        return None
    result = run([tool, "-convert", "txt", "-stdout", str(path)], timeout=120)
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def _soffice(path, target):
    """Headless LibreOffice conversion with a throwaway profile (read-only use)."""
    tool = find_tool("soffice")
    if not tool:
        return None
    with tempfile.TemporaryDirectory(prefix="wechat-portal-soffice-") as folder:
        profile = Path(folder) / "profile"
        result = run([tool, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                      "--convert-to", target, "--outdir", folder, str(path)], timeout=240)
        produced = [p for p in Path(folder).iterdir() if p.is_file()]
        if result.returncode or not produced:
            return None
        if target.startswith("pdf"):
            text, _info = _pdf(produced[0])
            return text if text.strip() else None
        text = _decode(produced[0].read_bytes())
        return text if text.strip() else None


def _utf16_scan(path):
    """Last resort for legacy Word files: recover UTF-16LE text runs, drop font-table noise."""
    data = path.read_bytes()[:64 * 1024 * 1024]
    lines = []
    for run_bytes in UTF16_RUN.findall(data):
        text = re.sub(r"[^\S\n]+", " ", run_bytes.decode("utf-16le", "ignore")).strip()
        if len(text) < 4:
            continue
        # Font and style tables look like "V一V伀V倀…": one character repeated at a fixed stride.
        most = max(text.count(ch) for ch in set(text))
        if most / len(text) > 0.3:
            continue
        lines.append(text)
    return "\n".join(lines) or None


def _ooxml_fallback(path, ext):
    """Minimal text for docx/pptx/xlsx when no converter is available."""
    names = {"docx": r"word/document\.xml", "pptx": r"ppt/slides/slide\d+\.xml", "xlsx": r"xl/sharedStrings\.xml"}
    pattern = names.get("xlsx" if ext == "xlsm" else ext)
    if not pattern:
        return None
    parts = []
    with zipfile.ZipFile(path) as archive:
        members = sorted((n for n in archive.namelist() if re.fullmatch(pattern, n)),
                         key=lambda n: int(re.sub(r"\D", "", n) or 0))
        for name in members[:500]:
            xml = archive.read(name)[:8 * 1024 * 1024].decode("utf-8", errors="replace")
            xml = re.sub(r"</(w:p|a:p|si)>", "\n", xml)
            parts.append(re.sub(r"<[^>]+>", "", xml))
    return "\n".join(parts) or None


def _zip_listing(path):
    with zipfile.ZipFile(path) as archive:
        rows = [f"- {i.filename}（{i.file_size} B）" for i in archive.infolist()[:500]]
    return "压缩包内容（未解压）：\n" + "\n".join(rows)


def extract_text(path, ext=None):
    """Return (text, info). info.method records which extractor produced the text."""
    path = Path(path)
    ext = (ext or path.suffix.lstrip(".")).lower()
    if ext in IMAGE_TYPES:
        raise Failure("DOCUMENT_UNSUPPORTED", "image file: open source_path directly")
    if ext not in SUPPORTED:
        raise Failure("DOCUMENT_UNSUPPORTED", ext or "unknown")
    try:
        if ext == "pdf":
            text, info = _pdf(path)
        elif ext == "zip":
            text, info = _zip_listing(path), {"method": "zip-listing"}
        elif ext in PLAIN_TYPES:
            text, info = _decode(path.read_bytes()[:16 * 1024 * 1024]), {"method": "plain"}
        else:
            text, info = None, {}
            chain = []
            if ext in MARKITDOWN_TYPES:
                chain.append(("markitdown", lambda: _markitdown(path)))
            if ext in TEXTUTIL_TYPES:
                chain.append(("textutil", lambda: _textutil(path)))
            if ext in SOFFICE_TEXT_TYPES:
                chain.append(("libreoffice", lambda: _soffice(path, "txt:Text")))
            if ext in SOFFICE_PDF_TYPES:
                chain.append(("libreoffice-pdf", lambda: _soffice(path, "pdf")))
            if ext in ("docx", "pptx", "xlsx", "xlsm"):
                chain.append(("ooxml-fallback", lambda: _ooxml_fallback(path, ext)))
            if ext in ("doc", "wps"):
                chain.append(("utf16-scan", lambda: _utf16_scan(path)))
            for method, attempt in chain:
                text = attempt()
                if text is not None:
                    info["method"] = method
                    info["approximate"] = method in ("ooxml-fallback", "utf16-scan")
                    break
            if text is None:
                raise Failure("DOCUMENT_EXTRACT_FAILED", ext)
    except (zipfile.BadZipFile, OSError):
        raise Failure("DOCUMENT_EXTRACT_FAILED", ext) from None
    text = text.replace("\r\n", "\n")
    info["chars"] = len(text)
    info["truncated"] = len(text) > MAX_DOC_CHARS
    return text[:MAX_DOC_CHARS], info


def render_pdf_pages(path, stem, pages, total=None):
    """Render the first N pages to PNG so the host can look at scans and slide PDFs."""
    tool = find_tool("pdftoppm")
    if not tool:
        raise Failure("TOOL_MISSING", "pdftoppm")
    last = pages if not total else min(pages, total)
    outputs = []
    with tempfile.TemporaryDirectory(prefix=".render-", dir=Path(stem).parent) as folder:
        prefix = Path(folder) / "page"
        result = run([tool, "-png", "-r", "110", "-f", "1", "-l", str(last), str(path), str(prefix)], timeout=240)
        if result.returncode:
            raise Failure("DOCUMENT_EXTRACT_FAILED", "pdftoppm")
        for index, produced in enumerate(sorted(Path(folder).glob("page-*.png")), 1):
            target = Path(f"{stem}-page-{index:02d}.png")
            write_new(target, produced.read_bytes())
            outputs.append(str(target))
    return outputs


def write_markdown(stem, title, source, text, info):
    header = [f"# {title}", "", f"- 来源：{source}", f"- 提取方式：{info.get('method')}"]
    if info.get("pages"):
        header.append(f"- 页数：{info['pages']}")
    if info.get("truncated"):
        header.append(f"- 正文已截断，仅保留前 {MAX_DOC_CHARS} 字符")
    target = Path(f"{stem}.md")
    write_new(target, ("\n".join(header) + "\n\n---\n\n" + text).encode())
    return str(target)
