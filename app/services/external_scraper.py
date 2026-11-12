"""Browser-automation layer for the external vendor-platform data source.

The target site is a specific instrument vendor's cloud platform that only
exposes its data through the web UI (no public API) — login form + a left
menu of a dozen-odd "databases", each with its own download button. This
module drives a real (headless) browser with Playwright to do exactly what
a person would do by hand: open the login page, fill in the account, click
in, go to each database's page, click "download", and grab the resulting
file.

Every selector used here (`username_selector`, `download_selector`, the
steps in `pre_download_steps`, ...) comes from ExternalSource /
ExternalDatabase config rows — this module has no built-in knowledge of any
particular site's HTML. Configure those selectors by inspecting the real
target site (right-click → inspect element on the login fields / submit
button / download button) and adjust them from the admin UI as the vendor's
front end changes; a version does not need a code change on this side.

Requires the `playwright` package plus its browser binaries:
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.core.config import settings
from app.models.external_source import ExternalDatabase, ExternalSource
from app.services.crypto_util import decrypt_secret

logger = logging.getLogger("aims.external_scraper")

DEFAULT_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 60_000


class ScraperError(RuntimeError):
    """Raised for any login/navigation/download failure, with a message
    specific enough to show directly in ExternalSyncLog.error_message."""


def _first_line(exc: BaseException) -> str:
    """Playwright 的错误信息带多行 Call log，界面上只要第一行。"""
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else exc.__class__.__name__


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        raise ScraperError(
            "Playwright 未安装或浏览器内核未下载。请在服务器上执行：\n"
            "  pip install playwright\n  playwright install chromium"
        ) from e


@contextmanager
def external_session(source: ExternalSource) -> Iterator["object"]:
    """Log into `source` and yield a Playwright Page with an authenticated
    session, ready for `download_from_database`. Closes the browser on
    exit (success or failure) either way.

    Raises ScraperError with a human-readable reason on any failure —
    caller is expected to store that string on ExternalSource/ExternalSyncLog
    and surface it in the admin UI rather than a raw traceback.
    """
    _require_playwright()
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeoutError
    from playwright.sync_api import sync_playwright

    password = decrypt_secret(source.password_encrypted)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=settings.PLAYWRIGHT_HEADLESS)
        except PWError as e:
            raise ScraperError(
                "无法启动浏览器内核（通常是还没执行 playwright install chromium）：" + _first_line(e)
            ) from e
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            try:
                page.goto(source.login_url, timeout=DEFAULT_TIMEOUT_MS, wait_until="load")
                page.fill(source.username_selector, source.username, timeout=DEFAULT_TIMEOUT_MS)
                page.fill(source.password_selector, password, timeout=DEFAULT_TIMEOUT_MS)
                page.click(source.submit_selector, timeout=DEFAULT_TIMEOUT_MS)

                if source.login_success_selector:
                    page.wait_for_selector(
                        source.login_success_selector, timeout=DEFAULT_TIMEOUT_MS
                    )
                else:
                    # Best-effort fallback when no explicit success marker is
                    # configured: just wait for the page to settle down after
                    # the submit navigation.
                    page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
            except PWTimeoutError as e:
                raise ScraperError(
                    f"登录 {source.name} 超时/失败：找不到元素或登录后未出现预期页面 "
                    f"({e}). 请检查 login_url / *_selector 配置是否与站点当前页面一致。"
                ) from e
            except PWError as e:
                # 证书错误 / 域名解析失败 / 连接被拒等：这是"URL 填错或站点不可达"最常见的形态，
                # 以前只捕获了 TimeoutError，这些会变成 500 + 原始堆栈。
                raise ScraperError(
                    f"无法打开或操作 {source.name} 的登录页：{_first_line(e)}。"
                    f"请检查 login_url 是否可访问（域名 / 证书 / 网络）以及 *_selector 配置。"
                ) from e

            yield page
        finally:
            context.close()
            browser.close()


def download_from_database(page, database: ExternalDatabase, download_dir: Path) -> Path:
    """Navigate to `database.list_url`, run any configured pre-download
    steps (setting a date range, clicking "query", etc.), click the download
    button, and save the resulting file under `download_dir`. Returns the
    saved file's path.
    """
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeoutError

    download_dir.mkdir(parents=True, exist_ok=True)

    try:
        page.goto(database.list_url, timeout=DEFAULT_TIMEOUT_MS, wait_until="load")

        for step in (database.pre_download_steps or []):
            action = step.get("action")
            selector = step.get("selector")
            if action == "fill":
                page.fill(selector, step.get("value", ""), timeout=DEFAULT_TIMEOUT_MS)
            elif action == "click":
                page.click(selector, timeout=DEFAULT_TIMEOUT_MS)
            elif action == "wait":
                if selector:
                    page.wait_for_selector(selector, timeout=DEFAULT_TIMEOUT_MS)
                else:
                    page.wait_for_timeout(int(step.get("ms", 1000)))
            else:
                logger.warning("Unknown pre_download_steps action %r for database %s, skipping", action, database.id)

        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            page.click(database.download_selector, timeout=DEFAULT_TIMEOUT_MS)
        download = download_info.value

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suggested = download.suggested_filename or f"database_{database.id}.{database.data_format}"
        save_path = download_dir / f"db{database.id}_{ts}_{suggested}"
        download.save_as(str(save_path))
        return save_path
    except PWTimeoutError as e:
        raise ScraperError(
            f"数据库「{database.name}」下载超时/失败：找不到元素或未触发下载 ({e})。"
            f"请检查 list_url / download_selector / pre_download_steps 配置。"
        ) from e
    except PWError as e:
        raise ScraperError(
            f"数据库「{database.name}」下载失败：{_first_line(e)}。"
            f"请检查 list_url 是否可访问以及 download_selector / pre_download_steps 配置。"
        ) from e
