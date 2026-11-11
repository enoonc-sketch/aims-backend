"""AIMS Template Package（.aimstpl）— 模板包格式与安全校验（计划书 12.3 / 12.5）。

一个 .aimstpl 就是一个 ZIP 容器：

    manifest.json    必需 —— 身份信息（uid/名称/分类/版本/最低兼容版本/作者/依赖）
    template.json    必需 —— 模板正文（参数、默认值、设备定义、报警规则等）
    schema.json      可选 —— 字段定义与校验规则
    preview.png      可选 —— 预览图
    README.md        可选 —— 使用说明
    assets/**        可选 —— 静态资源（只随包存取，不参与任何执行）
    scripts/**       可选 —— 扩展脚本：**只存储、默认禁用、全系统没有任何执行路径**

安全设计（12.9）：任何校验失败都返回带明确中文原因的 TemplatePackageError，
绝不静默降级 —— 计划书要求"导入失败时必须提供明确的错误原因"。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.deploy import APP_VERSION

logger = logging.getLogger("aims.templates")

#: 单个模板包上传体积上限（与部署配置 MAX_UPLOAD_BYTES 独立，模板不应超大）
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
#: 解压后总量的硬上限（防 zip 炸弹：64MB 压缩包声称解压出 2GB 的情形）
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
#: 单个成员上限
MAX_MEMBER_BYTES = 128 * 1024 * 1024
#: 单包最多成员数（防目录树轰炸）
MAX_MEMBERS = 2000

REQUIRED_FILES = ("manifest.json", "template.json")

MANIFEST_REQUIRED_FIELDS = {
    "uid": "模板唯一 ID",
    "name": "模板名称",
    "category": "模板分类",
    "version": "模板版本",
}


class TemplatePackageError(Exception):
    """模板包不合法。message 必须是可直接展示给用户的中文原因。"""


@dataclass
class TemplatePackage:
    """解析后的模板包内容。scripts 一律只保留文件名与字节，不存在执行语义。"""

    manifest: dict
    template: dict
    schema: dict | None = None
    readme: str | None = None
    preview_png: bytes | None = None
    assets: dict[str, bytes] = field(default_factory=dict)   # 相对路径 -> 字节
    scripts: dict[str, bytes] = field(default_factory=dict)  # 相对路径 -> 字节（禁用状态）
    sha256: str = ""
    size: int = 0


# --------------------------------------------------------------------------- #
def _norm_member(name: str) -> str:
    """归一化 ZIP 成员路径；任何穿越/绝对路径/反斜杠都直接拒绝。"""
    if "\\" in name:
        raise TemplatePackageError(f"模板包内包含非法路径（反斜杠）：{name!r}，疑似路径穿越攻击。")
    p = PurePosixPath(name)
    if p.is_absolute() or str(p).startswith("/"):
        raise TemplatePackageError(f"模板包内包含绝对路径：{name!r}。")
    parts = p.parts
    if any(part == ".." for part in parts):
        raise TemplatePackageError(f"模板包内包含 .. 上跳路径（路径穿越攻击）：{name!r}，拒绝导入。")
    if not parts:
        raise TemplatePackageError("模板包内包含空路径成员。")
    return str(p)


def _load_json(raw: bytes, filename: str) -> dict:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TemplatePackageError(f"{filename} 不是有效的 UTF-8 文本：{exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplatePackageError(f"{filename} JSON 解析失败（第 {exc.lineno} 行）：{exc.msg}") from None
    if not isinstance(data, dict):
        raise TemplatePackageError(f"{filename} 顶层必须是 JSON 对象。")
    return data


def _check_version_compatible(min_aims_version: str) -> None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(min_aims_version or "").strip())
    if not m:
        raise TemplatePackageError(f"manifest.min_aims_version 不是合法版本号：{min_aims_version!r}")
    cur_m = re.match(r"^(\d+)\.(\d+)\.(\d+)", APP_VERSION)
    cur = tuple(int(x) for x in cur_m.groups())
    need = tuple(int(x) for x in m.groups())
    if need > cur:
        raise TemplatePackageError(
            f"模板要求 AIMS ≥ {min_aims_version}，当前版本 {APP_VERSION} 不满足，已拒绝导入（避免半兼容状态）。")


def parse_package(data: bytes) -> TemplatePackage:
    """把 .aimstpl 字节流解析成 TemplatePackage；所有安全检查都在这里。"""
    if len(data) > MAX_PACKAGE_BYTES:
        raise TemplatePackageError(f"模板包超过大小上限（{MAX_PACKAGE_BYTES // 1024 // 1024} MB）。")
    if not data:
        raise TemplatePackageError("模板包为空文件。")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TemplatePackageError("文件不是有效的 ZIP/.aimstpl 容器。") from None

    names = zf.namelist()
    if len(names) > MAX_MEMBERS:
        raise TemplatePackageError(f"模板包成员数超过上限（{MAX_MEMBERS}）。")
    total = sum(info.file_size for info in zf.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        raise TemplatePackageError("模板包解压后体积异常庞大，疑似 zip 炸弹，已拒绝。")

    found: dict[str, bytes] = {}
    assets: dict[str, bytes] = {}
    scripts: dict[str, bytes] = {}
    preview_png: bytes | None = None
    for info in zf.infolist():
        name = _norm_member(info.filename)
        if info.is_dir():
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            raise TemplatePackageError(f"模板包内单文件过大：{name}。")
        raw = zf.read(info.filename)
        if name in REQUIRED_FILES:
            found[name] = raw
        elif name == "schema.json":
            found["schema.json"] = raw
        elif name == "README.md":
            found["README.md"] = raw
        elif name == "preview.png":
            preview_png = raw
        elif name.startswith("assets/"):
            assets[name[len("assets/"):]] = raw
        elif name.startswith("scripts/"):
            scripts[name[len("scripts/"):]] = raw
        # 其它未认识文件：忽略（向前兼容）

    missing = [f for f in REQUIRED_FILES if f not in found]
    if missing:
        raise TemplatePackageError(f"模板包缺少必需文件：{', '.join(missing)}。")

    manifest = _load_json(found["manifest.json"], "manifest.json")
    for key, label in MANIFEST_REQUIRED_FIELDS.items():
        if not str(manifest.get(key) or "").strip():
            raise TemplatePackageError(f"manifest.json 缺少必需字段：{label}（{key}）。")
    if not re.match(r"^[A-Za-z0-9._-]{4,64}$", str(manifest["uid"])):
        raise TemplatePackageError(f"模板唯一 ID 只允许字母/数字/点/下划线/连字符（4-64 位）：{manifest['uid']!r}")
    template = _load_json(found["template.json"], "template.json")
    schema = _load_json(found["schema.json"], "schema.json") if "schema.json" in found else None
    readme = found.get("README.md", b"").decode("utf-8-sig", errors="replace") or None

    _check_version_compatible(str(manifest.get("min_aims_version") or "0.0.0"))

    return TemplatePackage(
        manifest=manifest,
        template=template,
        schema=schema,
        readme=readme,
        preview_png=preview_png,
        assets=assets,
        scripts=scripts,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


# --------------------------------------------------------------------------- #
def build_package(*, manifest: dict, template: dict, schema: dict | None = None,
                  readme: str | None = None, preview_png: bytes | None = None,
                  assets: dict[str, bytes] | None = None) -> bytes:
    """把模板内容打包成 .aimstpl 字节流（导出用）。"""
    for key in ("uid", "name", "category", "version"):
        if not str(manifest.get(key) or "").strip():
            raise TemplatePackageError(f"导出失败：manifest 缺少必需字段 {key}。")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("template.json", json.dumps(template, ensure_ascii=False, indent=2))
        if schema is not None:
            zf.writestr("schema.json", json.dumps(schema, ensure_ascii=False, indent=2))
        if readme:
            zf.writestr("README.md", readme)
        if preview_png:
            zf.writestr("preview.png", preview_png)
        for rel, data in (assets or {}).items():
            zf.writestr(f"assets/{_norm_member(rel)}", data)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
def iter_bundle(data: bytes) -> list[tuple[str, bytes]]:
    """批量模板包：一个普通 ZIP 里面装了若干 .aimstpl。

    返回 [(内部文件名, 模板包字节)]；对每个成员同样做路径穿越检查。
    上传文件本身是 .aimstpl 时不要调用本函数。
    """
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise TemplatePackageError("文件不是有效的 ZIP 容器。")
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in zf.namelist() if n.lower().endswith(".aimstpl") and not n.endswith("/")]
    if not names:
        raise TemplatePackageError("批量包内没有找到任何 .aimstpl 模板文件。")
    out: list[tuple[str, bytes]] = []
    for n in names:
        _norm_member(n)
        out.append((n, zf.read(n)))
    return out
