"""前端中英文一致性审计（静态解析 frontend.html，不需要浏览器）。

防的是几类真实出过的问题：
- 侧栏按钮与翻译词条按"位置"对应，漏一项就整体错位（界面里出现重复的英文 Cloud Data / System）；
- en / zh 词条键不一致，切换语言后出现空白或回退成键名；
- 提示语里写死 "System → Login" 这类英文菜单名，中文界面里夹英文；
- 静态对照表 I18N_PAIRS 出现空译文或同一英文键对应多个中文。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")


def _dict_keys(block: str) -> set[str]:
    # 字典里一行常写多个键（a: 'x', b: 'y'），所以不能只认行首
    return set(re.findall(r"""(?:^|[,{])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*['"]""", block, re.M))


def _translation_blocks() -> tuple[str, str]:
    start = HTML.index("const translations = {")
    en_at = HTML.index("\n  en: {", start)
    zh_at = HTML.index("\n  zh: {", en_at)
    end = HTML.index("\n};", zh_at)
    return HTML[en_at:zh_at], HTML[zh_at:end]


def test_en_zh_dictionary_keys_match():
    en, zh = _translation_blocks()
    ke, kz = _dict_keys(en), _dict_keys(zh)
    assert ke, "没有解析到英文词条"
    assert ke == kz, f"en/zh 词条键不一致：仅英文={sorted(ke - kz)}，仅中文={sorted(kz - ke)}"


def test_every_used_key_exists():
    en, zh = _translation_blocks()
    keys = _dict_keys(en)
    used = set(re.findall(r"""data-i18n="([A-Za-z0-9_]+)"?""", HTML))
    used |= set(re.findall(r"""\bt\('([A-Za-z0-9_]+)'\)""", HTML))
    used |= set(re.findall(r"""\btf\('([A-Za-z0-9_]+)'""", HTML))
    missing = sorted(k for k in used if k not in keys)
    assert not missing, f"使用了但未定义的词条：{missing}"


def test_nav_buttons_are_key_driven():
    """每个侧栏按钮都必须自带 data-i18n，禁止回到"按位置映射"。"""
    nav = re.findall(r'<button[^>]*class="nav-btn[^"]*"[^>]*>', HTML)
    assert len(nav) >= 14
    assert all("data-i18n=" in b for b in nav), "存在没有 data-i18n 的侧栏按钮"
    assert "var pageIds = [" not in HTML


def test_no_english_menu_path_inside_chinese_text():
    assert "（System → Login）" not in HTML, "中文提示里不要写死英文菜单名，请走 t('sessionExpired') 等词条"


def test_i18n_pairs_are_wellformed():
    m = re.search(r"var I18N_PAIRS = (\[.*?\n\]);", HTML, re.S)
    assert m, "找不到 I18N_PAIRS"
    if not shutil.which("node"):
        pytest.skip("需要 node 来解析 JS 数组")
    out = subprocess.run(
        ["node", "-e", f"console.log(JSON.stringify({m.group(1)}))"],
        capture_output=True, text=True, check=True,
    ).stdout
    pairs = json.loads(out)
    assert len(pairs) > 200
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    seen_en: dict[str, str] = {}
    seen_zh: dict[str, str] = {}
    for p in pairs:
        en, zh = norm(p[0]), norm(p[1])
        assert en and zh, f"空译文：{p}"
        assert re.search(r"[\u4e00-\u9fff]", zh), f"中文侧没有中文：{p}"
        assert seen_en.setdefault(en, zh) == zh, f"英文键重复且译文不同：{en!r}"
        assert seen_zh.setdefault(zh, en) == en, f"中文键重复且译文不同：{zh!r}"
