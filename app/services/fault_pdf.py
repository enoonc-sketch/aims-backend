"""故障分析 PDF 解析（AIMS 1.0 阶段 7，替换前端假页签）。

前端 Maintenance → PDF Import 以前只是把上传文件名拼进一句"检测到 3 个故障模式"
的假结果。这里实现真实解析：

- 用 pypdf 提取文本层（纯 Python、离线可用、无系统依赖）；
- 从文本里提取可核对的结构化信息：设备/位号、故障关键词、报警码、测量值+单位；
- 命中知识库规则时给出候选诊断（复用 ai_cores 的规则匹配），但**只作为建议**，
  不写库、不执行任何动作；
- 扫描件（无文本层）如实返回失败并说明需要 OCR，绝不编造内容。
"""
from __future__ import annotations

import io
import re
from typing import Any

# 常见工业故障/报警关键词（中英），用于从自由文本里定位问题段落
FAULT_KEYWORDS = (
    "fault", "failure", "failed", "error", "alarm", "warning", "trip", "shutdown",
    "leak", "drift", "noise", "blocked", "overheat", "over range", "out of range",
    "故障", "报警", "异常", "跳车", "联锁", "泄漏", "漂移", "堵塞", "过热", "超限",
    "失效", "偏差",
)

# 报警码：A-102 / ALM123 / E-45 之类
_CODE_RE = re.compile(r"\b(?:[A-Z]{1,4}[-_ ]?\d{1,6})\b")
# 测量值 + 单位
_VALUE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(ppm|ppb|%|vol%|mol%|°C|℃|K|bar|kPa|MPa|psi|mA|V|L/min|mL/min|s|min|h)\b",
    re.IGNORECASE,
)
# 设备位号：常见 analyzer tag 形态
_TAG_RE = re.compile(r"\b([A-Z]{1,6}[-_]\d{2,5}[A-Z]?)\b")

MAX_PAGES = 50


class PdfParseError(Exception):
    """PDF 无法解析（加密、损坏、非 PDF）。"""


def extract_text(raw: bytes, max_pages: int = MAX_PAGES) -> tuple[str, int, list[str]]:
    """提取 PDF 文本层。返回 (text, pages, warnings)。"""
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - pypdf 是硬依赖，缺失说明打包不全
        raise PdfParseError(f"缺少 PDF 解析组件 pypdf：{exc}") from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise PdfParseError(f"不是有效的 PDF 文件：{exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise PdfParseError("PDF 已加密，无法解析")

    warnings: list[str] = []
    pages = len(reader.pages)
    if pages > max_pages:
        warnings.append(f"PDF 共 {pages} 页，仅解析前 {max_pages} 页")
    chunks: list[str] = []
    blank = 0
    for idx, page in enumerate(reader.pages[:max_pages]):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"第 {idx + 1} 页解析失败：{exc}")
            continue
        if text.strip():
            chunks.append(text)
        else:
            blank += 1
    text = "\n".join(chunks).strip()
    if not text:
        raise PdfParseError(
            "该 PDF 没有文本层（可能是扫描件或图片），无法直接解析；"
            "请先做 OCR，或改用结构化导入（CSV/Excel）"
        )
    if blank:
        warnings.append(f"{blank} 页没有可提取文本（可能是图片页）")
    return text, pages, warnings


def analyze_text(text: str) -> dict[str, Any]:
    """从文本中抽取可核对的结构化信息。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    patterns: list[dict[str, Any]] = []
    for ln in lines:
        low = ln.lower()
        hits = [kw for kw in FAULT_KEYWORDS if kw in low]
        if not hits:
            continue
        patterns.append({
            "line": ln[:400],
            "keywords": hits[:5],
            "codes": sorted(set(_CODE_RE.findall(ln)))[:5],
            "values": [f"{v} {u}" for v, u in _VALUE_RE.findall(ln)][:5],
        })
        if len(patterns) >= 50:  # 上限，避免超长报告拖垮响应
            break

    values = [{"value": float(v), "unit": u} for v, u in _VALUE_RE.findall(text)][:100]
    tags = sorted({t for t in _TAG_RE.findall(text)})[:20]
    codes = sorted({c for c in _CODE_RE.findall(text)})[:50]
    keyword_counts: dict[str, int] = {}
    low = text.lower()
    for kw in FAULT_KEYWORDS:
        n = low.count(kw)
        if n:
            keyword_counts[kw] = n

    return {
        "patterns": patterns,
        "values": values,
        "tags": tags,
        "codes": codes,
        "keyword_counts": keyword_counts,
        "line_count": len(lines),
    }


def suggest_diagnoses(text: str, component_name: str = "") -> list[dict[str, Any]]:
    """用内置规则包给候选诊断（仅建议，不落库）。"""
    try:
        from app.services.ai_cores import match_diagnosis
    except Exception:
        return []
    out = []
    low = text.lower()
    for kw in FAULT_KEYWORDS:
        if kw in low:
            try:
                match = match_diagnosis(kw, component_name)
            except Exception:
                continue
            if match:
                out.append({"trigger": kw, "rule": match})
            if len(out) >= 5:
                break
    return out


def analyze_pdf(raw: bytes, filename: str = "", component_name: str = "") -> dict[str, Any]:
    text, pages, warnings = extract_text(raw)
    parsed = analyze_text(text)
    return {
        "ok": True,
        "filename": filename,
        "pages": pages,
        "chars": len(text),
        "text_excerpt": text[:4000],
        "patterns": parsed["patterns"],
        "values": parsed["values"],
        "tags": parsed["tags"],
        "codes": parsed["codes"],
        "keyword_counts": parsed["keyword_counts"],
        "line_count": parsed["line_count"],
        "diagnosis_suggestions": suggest_diagnoses(text, component_name),
        "warnings": warnings,
        "note": "解析结果仅作故障分析参考，不会自动写入数据库或创建工单",
    }
