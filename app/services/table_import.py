"""Shared table-import engine used by every module's `/import` endpoint
(Devices, Communication/Gateways, Data Analysis, Knowledge, Maintenance,
Assets, Lab).

Gives every module the same forgiving auto-recognition behaviour that
`app/api/v1/measurements.py` already has: read an uploaded CSV/Excel file,
auto-detect which column is which field by matching a bilingual (EN/CN)
list of header candidates per field (case-insensitive, exact match first
then substring), and hand back clean per-field values ready to build ORM
rows from — instead of every module hand-rolling its own column-sniffing.
"""
from __future__ import annotations

import io
from typing import Any, Optional

MAX_IMPORT_BYTES = 20 * 1024 * 1024  # legacy constant; the enforced limit is settings.MAX_UPLOAD_BYTES (see upload_security)

SUPPORTED_EXTENSIONS = ("csv", "xlsx", "xls")


def read_table(raw: bytes, filename: str):
    """Parse an uploaded CSV/Excel file into a pandas DataFrame.

    CSV encoding is tried in order utf-8-sig -> gbk -> utf-8: a lot of
    spreadsheets exported from Chinese-locale Excel/WPS are actually GBK,
    not UTF-8, and silently mis-decoding them (mojibake instead of an error)
    is a common real-world cause of "auto-detect didn't recognize my
    columns" — this makes that case fail over instead of garbling headers.

    Every failure mode here — bad encoding, malformed/empty CSV, a corrupt or
    password-protected workbook, a legacy .xls file when the optional `xlrd`
    engine isn't installed, an .xlsx when `openpyxl` isn't installed, etc. —
    is normalized to ValueError, never left to escape as some other
    exception type. Every /import endpoint only catches ValueError from this
    call; anything else previously fell through as an unhandled 500.
    """
    import pandas as pd

    from app.services import upload_security as _us

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Only {'/'.join('.' + e for e in SUPPORTED_EXTENSIONS)} files are supported")

    # Defence in depth: every caller (HTTP upload, scraped download, ...) gets the
    # same content sniffing / zip-bomb / macro checks and row+column caps.
    limits = _us.default_limits()
    try:
        _us.inspect_table_bytes(raw, ext, limits)
    except _us.UploadRejected as e:
        raise ValueError(e.detail) from e

    def _capped(df):
        try:
            _us.enforce_table_limits(len(df), len(df.columns), limits)
        except _us.UploadRejected as e:
            raise ValueError(e.detail) from e
        return df

    if ext == "csv":
        last_err: Optional[Exception] = None
        for encoding in ("utf-8-sig", "gbk", "utf-8"):
            try:
                df = pd.read_csv(io.BytesIO(raw), encoding=encoding, nrows=limits.max_rows + 1)
            except UnicodeDecodeError as e:
                last_err = e
                continue
            except Exception as e:
                # malformed CSV (bad quoting, ragged rows, etc.) — not an
                # encoding problem, so no point trying the other encodings
                raise ValueError(f"Could not parse CSV file: {e}") from e
            return _capped(df)
        raise ValueError(f"Could not decode CSV file (tried utf-8-sig/gbk/utf-8): {last_err}")

    try:
        df = pd.read_excel(io.BytesIO(raw))
    except ImportError as e:
        # optional engine (openpyxl for .xlsx, xlrd for legacy .xls) missing
        raise ValueError(
            f"Server is missing the Excel engine needed to read .{ext} files ({e}). "
            "Try saving as .csv instead, or ask an admin to install it."
        ) from e
    except Exception as e:
        raise ValueError(f"Could not parse {filename}: this may not be a valid .{ext} file ({e})") from e
    return _capped(df)


def pick_column(columns, candidates: tuple[str, ...]) -> Optional[str]:
    """Exact (case-insensitive) match first, then substring match — the
    latter catches headers like "设备编号(Device Code)" or "Name / 名称"
    that wrap the plain candidate in extra text."""
    lower_map = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    for cand in candidates:
        for lc, orig in lower_map.items():
            if cand in lc:
                return orig
    return None


def detect_columns(columns, spec: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """spec: field_name -> candidate header names (lowercase). Returns
    field_name -> actual column name in `columns`, for every field found."""
    out: dict[str, str] = {}
    for field, candidates in spec.items():
        col = pick_column(columns, candidates)
        if col is not None:
            out[field] = col
    return out


def clean_str(v: Any) -> Optional[str]:
    import pandas as pd

    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def clean_number(v: Any) -> Optional[float]:
    import pandas as pd

    n = pd.to_numeric(v, errors="coerce")
    if pd.isna(n):
        return None
    return float(n)


def clean_int(v: Any) -> Optional[int]:
    n = clean_number(v)
    return int(n) if n is not None else None


def clean_date(v: Any):
    import pandas as pd

    d = pd.to_datetime(v, errors="coerce")
    if pd.isna(d):
        return None
    return d.date()


def clean_datetime(v: Any):
    import pandas as pd

    d = pd.to_datetime(v, errors="coerce", utc=True)
    if pd.isna(d):
        return None
    return d.to_pydatetime()


def row_value(row, columns_detected: dict[str, str], field: str):
    """Fetch a raw cell for `field` from a pandas Series `row`, or None if
    that field's column wasn't detected in this file at all."""
    col = columns_detected.get(field)
    if col is None:
        return None
    return row[col]


def build_excel_template(headers: list[str], example_rows: Optional[list[list]] = None) -> bytes:
    """Build a minimal .xlsx template file: a header row (matching the
    Chinese/English column names every /import endpoint auto-detects) plus
    zero or more filled-in example rows, so a user downloading it sees
    exactly what to put in each column instead of guessing. Column widths
    are auto-sized to the content so the template opens readable, not with
    every column crushed to Excel's default width.
    """
    import pandas as pd

    buf = io.BytesIO()
    df = pd.DataFrame(example_rows or [], columns=headers)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
        ws = writer.sheets["Sheet1"]
        for col_idx, header in enumerate(headers, start=1):
            longest = len(str(header))
            for r in example_rows or []:
                if col_idx - 1 < len(r):
                    longest = max(longest, len(str(r[col_idx - 1])))
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(10, longest + 4)
    return buf.getvalue()


TEMPLATE_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def template_headers(*, filename: str) -> dict[str, str]:
    """Content-Disposition header for a template download response.

    `filename` here is Chinese (设备导入模板.xlsx etc.), and raw HTTP header
    values are latin-1 only — passing it straight through blows up with
    UnicodeEncodeError. RFC 6266's filename* parameter carries a UTF-8 name
    as percent-encoded ASCII; a plain ASCII fallback filename covers any
    client that doesn't understand filename*.
    """
    import urllib.parse

    quoted = urllib.parse.quote(filename)
    return {"Content-Disposition": f"attachment; filename=\"import_template.xlsx\"; filename*=UTF-8''{quoted}"}
