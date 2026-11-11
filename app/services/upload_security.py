"""Upload safety checks shared by every file-accepting endpoint.

Layers (all applied before any parser touches the bytes):
  1. request-level body cap            -> RequestSizeLimitMiddleware
  2. file name sanitising              -> sanitize_filename()
  3. extension whitelist               -> read_upload(allowed_exts=...)
  4. size-capped, chunked read         -> read_upload()  (never reads more than the cap)
  5. content sniffing vs. claimed type -> inspect_table_bytes()
       .xlsx : must be a well formed ZIP/OOXML package; zip-bomb limits (entry count,
               total uncompressed size, compression ratio); no encrypted entries, no
               path-traversal names, no VBA macros / embedded OLE / ActiveX /
               external links / data connections
       .xls  : must carry the OLE2 signature
       .csv  : must be text (no NUL / binary signatures, decodable as UTF-8 or GBK)
  6. shape limits after parsing        -> enforce_table_limits()
  7. audit                             -> file name / size / sha256 (or the rejection
                                          reason) are attached to the request's audit record

The core is plain Python (`UploadRejected` carries the HTTP status); only
`read_upload` translates it to FastAPI's HTTPException.
"""
from __future__ import annotations

import codecs
import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any

TABLE_EXTS = ("csv", "xlsx", "xls")
_CHUNK = 1024 * 1024
_MB = 1024 * 1024


class UploadRejected(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class UploadLimits:
    max_bytes: int = 20 * _MB
    max_rows: int = 500_000
    max_columns: int = 500
    max_xlsx_uncompressed: int = 200 * _MB
    max_xlsx_entries: int = 2000
    max_xlsx_ratio: int = 200


@dataclass(frozen=True)
class SecureUpload:
    raw: bytes
    filename: str      # sanitised
    ext: str
    size: int
    sha256: str


def default_limits() -> UploadLimits:
    from app.core.config import settings

    return UploadLimits(
        max_bytes=settings.MAX_UPLOAD_BYTES,
        max_rows=settings.MAX_UPLOAD_ROWS,
        max_columns=settings.MAX_UPLOAD_COLUMNS,
        max_xlsx_uncompressed=settings.MAX_XLSX_UNCOMPRESSED_BYTES,
        max_xlsx_entries=settings.MAX_XLSX_ENTRIES,
        max_xlsx_ratio=settings.MAX_XLSX_COMPRESSION_RATIO,
    )


# --------------------------------------------------------------------------- #
# file names
# --------------------------------------------------------------------------- #
_UNSAFE_CHARS = re.compile(r"[^\w.\- ()\[\]]", re.UNICODE)


def sanitize_filename(name: str | None, max_len: int = 100) -> str:
    """Return a display/storage-safe file name: no directories, no control or markup
    characters, no leading dots, bounded length. The extension is preserved."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = _UNSAFE_CHARS.sub("_", name)
    name = re.sub(r"\.{2,}", ".", name).strip(" .")
    if not name:
        return "upload"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def file_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# --------------------------------------------------------------------------- #
# content inspection
# --------------------------------------------------------------------------- #
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_LOCAL = b"PK\x03\x04"
_BINARY_SIGNATURES = (
    _OLE2, b"PK\x03\x04", b"PK\x05\x06", b"MZ", b"\x7fELF", b"%PDF", b"GIF8",
    b"\x89PNG", b"\xff\xd8\xff", b"\x1f\x8b", b"BZh", b"7z\xbc\xaf", b"Rar!", b"\xca\xfe\xba\xbe",
)
_FORBIDDEN_XLSX_PREFIXES = (
    "xl/vbaproject", "xl/embeddings/", "xl/externallinks/", "xl/activex/", "xl/ctrlprops/",
)
_FORBIDDEN_XLSX_NAMES = {"xl/connections.xml"}


def _reject(detail: str, status: int = 400):
    raise UploadRejected(status, detail)


def _inspect_xlsx(raw: bytes, limits: UploadLimits) -> None:
    if not raw.startswith(_ZIP_LOCAL):
        _reject("File content is not a valid .xlsx workbook (bad signature)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        _reject("File content is not a valid .xlsx workbook (corrupt archive)")
    if len(infos) > limits.max_xlsx_entries:
        _reject("Workbook archive has too many entries")

    total = 0
    names: set[str] = set()
    for info in infos:
        name = info.filename
        parts = name.replace("\\", "/").split("/")
        if name.startswith("/") or "\\" in name or ".." in parts or "\x00" in name:
            _reject("Workbook archive contains unsafe paths")
        if info.flag_bits & 0x1:
            _reject("Encrypted or password-protected workbooks are not accepted")
        total += info.file_size
        if total > limits.max_xlsx_uncompressed:
            _reject(
                f"Workbook expands to more than {limits.max_xlsx_uncompressed // _MB} MB "
                "when decompressed", 413,
            )
        if info.file_size > _MB and info.compress_size > 0 and info.file_size / info.compress_size > limits.max_xlsx_ratio:
            _reject("Workbook archive has a suspicious compression ratio")
        low = name.lower()
        names.add(low)
        if low.startswith(_FORBIDDEN_XLSX_PREFIXES) or low in _FORBIDDEN_XLSX_NAMES or low.endswith("vbaproject.bin"):
            _reject("Workbooks containing macros, embedded objects or external links are not accepted")

    if "[content_types].xml" not in names or "xl/workbook.xml" not in names:
        _reject("File content is not a valid .xlsx workbook (missing OOXML parts)")


def _inspect_xls(raw: bytes) -> None:
    if not raw.startswith(_OLE2):
        _reject("File content is not a valid legacy .xls workbook (bad signature)")


def _inspect_csv(raw: bytes) -> None:
    head = raw[: 64 * 1024]
    if b"\x00" in head:
        _reject("File content is not a text CSV file (binary data detected)")
    if any(head.startswith(sig) for sig in _BINARY_SIGNATURES):
        _reject("File content is not a text CSV file (binary format detected)")
    ctrl = sum(1 for b in head if b < 0x20 and b not in (0x09, 0x0A, 0x0D, 0x0C)) + head.count(b"\x7f")
    if head and ctrl / len(head) > 0.005:
        _reject("File content is not a text CSV file (control characters detected)")
    for enc in ("utf-8-sig", "gbk"):
        try:
            codecs.getincrementaldecoder(enc)().decode(head, final=False)  # tolerates a cut multibyte tail
            return
        except UnicodeDecodeError:
            continue
    _reject("CSV text encoding is not supported (use UTF-8 or GBK)")


def inspect_table_bytes(raw: bytes, ext: str, limits: UploadLimits | None = None) -> None:
    """Verify that `raw` really is what its extension claims. Raises UploadRejected."""
    limits = limits or default_limits()
    if ext == "xlsx":
        _inspect_xlsx(raw, limits)
    elif ext == "xls":
        _inspect_xls(raw)
    elif ext == "csv":
        _inspect_csv(raw)
    elif ext == "pdf":
        _inspect_pdf(raw)
    else:
        _reject(f"Unsupported file type .{ext}")


def _inspect_pdf(raw: bytes) -> None:
    """PDF 魔数校验：合法 PDF 以 %PDF- 开头（允许少量前导空白/垃圾字节）。

    同时拒绝包含可执行脚本入口（/JS、/Launch、/OpenAction）的 PDF——这类文件
    在阅读器里可自动执行动作，不应进入解析流程（总方案 3.5 文件安全）。
    """
    head = raw[:1024]
    if b"%PDF-" not in head:
        _reject("File content is not a valid PDF")
    for marker in (b"/JavaScript", b"/Launch", b"/OpenAction"):
        if marker in raw:
            _reject("PDF 包含可执行动作（/JavaScript、/Launch 等），出于安全考虑拒绝解析")


def enforce_table_limits(rows: int, columns: int, limits: UploadLimits | None = None) -> None:
    limits = limits or default_limits()
    if rows > limits.max_rows:
        raise UploadRejected(413, f"Too many rows (max {limits.max_rows})")
    if columns > limits.max_columns:
        raise UploadRejected(413, f"Too many columns (max {limits.max_columns})")


def csv_row_cap() -> int:
    """`nrows` value for pandas.read_csv so a huge CSV is never fully materialised
    (one extra row lets enforce_table_limits notice the overflow)."""
    return default_limits().max_rows + 1


def check_dataframe(df: Any, limits: UploadLimits | None = None) -> None:
    """HTTP-facing shape check for an already parsed DataFrame."""
    from fastapi import HTTPException

    try:
        enforce_table_limits(len(df), len(df.columns), limits)
    except UploadRejected as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


# --------------------------------------------------------------------------- #
# FastAPI-facing entry point
# --------------------------------------------------------------------------- #
async def read_upload(
    file: Any,
    *,
    allowed_exts: tuple[str, ...] = TABLE_EXTS,
    limits: UploadLimits | None = None,
) -> SecureUpload:
    """Validate and read an `UploadFile`. Raises HTTPException(400/413) on any
    violation; never buffers more than `limits.max_bytes` (+1 chunk)."""
    from fastapi import HTTPException

    from app.core import audit_context

    limits = limits or default_limits()
    safe_name = sanitize_filename(getattr(file, "filename", None))
    ext = file_extension(safe_name)
    size = 0
    try:
        if ext not in allowed_exts:
            _reject("Only " + ", ".join("." + e for e in allowed_exts) + " files are supported")
        chunks: list[bytes] = []
        while True:
            chunk = await file.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limits.max_bytes:
                _reject(f"File too large (max {limits.max_bytes // _MB} MB)", 413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            _reject("File is empty")
        inspect_table_bytes(raw, ext, limits)
    except UploadRejected as e:
        audit_context.note(audit_detail={"upload_rejected": {"filename": safe_name, "size": size, "reason": e.detail}})
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    digest = hashlib.sha256(raw).hexdigest()
    audit_context.note(upload={"filename": safe_name, "size": len(raw), "sha256": digest})
    return SecureUpload(raw=raw, filename=safe_name, ext=ext, size=len(raw), sha256=digest)
