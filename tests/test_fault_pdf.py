"""阶段 7：故障分析 PDF 真实解析（替换前端假结果）。"""
import io

import pytest

from app.services import fault_pdf


def _make_pdf(text: str) -> bytes:
    """用 pypdf 的空白页 + reportlab 生成带文本层的 PDF。"""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in text.splitlines():
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


def test_extract_and_analyze_real_pdf():
    pdf = _make_pdf(
        "Fault report for analyzer AT-104\n"
        "Alarm A-102 active: detector drift 12.5 ppm over range\n"
        "Root cause: sample line blocked, pressure 0.3 bar\n"
    )
    out = fault_pdf.analyze_pdf(pdf, filename="fault.pdf")
    assert out["ok"] is True and out["pages"] >= 1
    assert "AT-104" in out["tags"]
    assert any("A-102" in c for c in out["codes"])
    assert out["patterns"]  # 至少命中一行故障描述
    assert any(v["unit"].lower() == "ppm" for v in out["values"])
    assert out["keyword_counts"].get("fault") or out["keyword_counts"].get("alarm")


def test_blank_pdf_is_rejected_honestly():
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(fault_pdf.PdfParseError) as exc:
        fault_pdf.extract_text(buf.getvalue())
    assert "OCR" in str(exc.value) or "文本层" in str(exc.value)


def test_non_pdf_bytes_raise_parse_error():
    with pytest.raises(fault_pdf.PdfParseError):
        fault_pdf.extract_text(b"this is not a pdf at all")


def test_endpoint_analyzes_upload(client):
    pdf = _make_pdf("Pump P-201 vibration alarm, bearing temperature 85 C\n")
    r = client.post(
        "/api/v1/maintenance/fault-pdf/analyze",
        files={"file": ("report.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["chars"] > 0
    assert "P-201" in body["tags"]


def test_endpoint_rejects_fake_extension(client):
    r = client.post(
        "/api/v1/maintenance/fault-pdf/analyze",
        files={"file": ("evil.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_endpoint_rejects_pdf_with_javascript(client):
    pdf = _make_pdf("hello")
    evil = pdf.replace(b"/Type /Page", b"/JavaScript (app.alert(1)) /Type /Page", 1)
    r = client.post(
        "/api/v1/maintenance/fault-pdf/analyze",
        files={"file": ("evil.pdf", evil, "application/pdf")},
    )
    assert r.status_code == 400
    assert "可执行动作" in r.json()["detail"]
