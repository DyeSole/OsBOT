from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser"
AUTH_DIR = DATA_DIR / "auth"

QR_SELECTORS: dict[str, list[str]] = {
    "bilibili": [
        ".login-scan-box img",
        ".qrcode-box img",
        ".qr-code__image",
        "canvas",
    ],
    "xiaohongshu": [
        '[class*="qrcode"] img',
        '[class*="qr"] img',
        '[class*="login"] canvas',
        '[class*="qrcode"] canvas',
    ],
}


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def list_profiles() -> list[str]:
    _ensure_dirs()
    return sorted(p.stem for p in AUTH_DIR.glob("*.json"))


def _looks_logged_in_url(current_url: str, login_url: str) -> bool:
    current = current_url.lower()
    login = login_url.lower()
    return current != login and "login" not in current


async def _is_logged_in(page) -> bool:
    login_selectors = [
        'button:has-text("登录")',
        'button:has-text("Log in")',
        'a:has-text("登录")',
        'a:has-text("Sign in")',
        'input[placeholder*="手机号"]',
        'input[placeholder*="验证码"]',
        'input[type="password"]',
    ]
    for selector in login_selectors:
        try:
            locator = page.locator(selector)
            if await locator.first().is_visible():
                return False
        except Exception:
            continue
    return True


async def _capture_login_preview(page, profile: str) -> bytes:
    selectors = QR_SELECTORS.get(profile, [])
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                return await locator.screenshot()
        except Exception:
            continue
    return await page.screenshot(full_page=True)


async def start_login_session(profile: str, url: str) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    _ensure_dirs()
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url)
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(2)
    preview = await _capture_login_preview(page, profile)
    auth_path = AUTH_DIR / f"{profile}.json"
    return {
        "playwright": playwright,
        "browser": browser,
        "context": context,
        "page": page,
        "auth_path": auth_path,
        "preview": preview,
    }


async def finish_login_session(
    session: dict[str, Any],
    profile: str,
    *,
    login_url: str,
    timeout_ms: int = 120_000,
    poll_interval_ms: int = 5_000,
) -> str:
    page = session["page"]
    context = session["context"]
    auth_path = session["auth_path"]

    elapsed_ms = 0
    logged_in = False
    while elapsed_ms < timeout_ms:
        await asyncio.sleep(poll_interval_ms / 1000)
        elapsed_ms += poll_interval_ms
        current_url = page.url
        if _looks_logged_in_url(current_url, login_url) and await _is_logged_in(page):
            logged_in = True
            break

    if not logged_in:
        return f"等待登录超时（{timeout_ms // 1000}s），未保存 {profile} 登录态。"

    await context.storage_state(path=str(auth_path))
    return f"已保存 {profile} 登录态 -> {auth_path}"


async def close_login_session(session: dict[str, Any]) -> None:
    page = session.get("page")
    context = session.get("context")
    browser = session.get("browser")
    playwright = session.get("playwright")

    try:
        if page is not None:
            await page.close()
    except Exception:
        pass
    try:
        if context is not None:
            await context.close()
    except Exception:
        pass
    try:
        if browser is not None:
            await browser.close()
    except Exception:
        pass
    try:
        if playwright is not None:
            await playwright.stop()
    except Exception:
        pass
