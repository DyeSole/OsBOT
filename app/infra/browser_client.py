from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser"
AUTH_DIR = DATA_DIR / "auth"

DOMAIN_PROFILE: dict[str, str] = {
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",
}

BILIBILI_DOMAINS = {"bilibili.com", "b23.tv"}

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

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


def _is_bilibili(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BILIBILI_DOMAINS)


def _match_browser_profile(url: str) -> str | None:
    host = urlparse(url).hostname or ""
    for domain, profile in DOMAIN_PROFILE.items():
        if host == domain or host.endswith("." + domain):
            auth_path = AUTH_DIR / f"{profile}.json"
            if auth_path.exists():
                return profile
    return None


def extract_urls(text: str) -> list[str]:
    return [u for u in URL_PATTERN.findall(text) if _is_bilibili(u) or _match_browser_profile(u)]


async def fetch_page_content(url: str) -> str | None:
    if _is_bilibili(url):
        from app.infra.bilibili_client import fetch_bilibili
        return await fetch_bilibili(url)

    return await _fetch_via_browser(url)


XHS_SELECTORS = {
    "title": "#detail-title, .title, .note-title",
    "content": "#detail-desc .note-text, .desc, .note-content, .content",
    "likes": ".like-count, .interactions .count",
}


async def _extract_xhs(page) -> str:
    parts: list[str] = []
    for key, sel in XHS_SELECTORS.items():
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=2000):
                text = (await locator.inner_text()).strip()
                if text:
                    if key == "title":
                        parts.append(f"标题: {text}")
                    elif key == "content":
                        parts.append(text[:500])
                    elif key == "likes":
                        parts.append(f"点赞: {text}")
        except Exception:
            continue

    comments: list[str] = []
    try:
        items = page.locator(".comment-item, .comment-inner, [class*='comment'] .content")
        count = await items.count()
        for i in range(min(count, 3)):
            text = (await items.nth(i).inner_text()).strip()
            if text:
                comments.append(f"  {text[:100]}")
    except Exception:
        pass
    if comments:
        parts.append("热门评论:")
        parts.extend(comments)

    return "\n".join(parts)


async def _fetch_via_browser(url: str) -> str | None:
    from playwright.async_api import async_playwright

    profile = _match_browser_profile(url)
    if not profile:
        return None

    _ensure_dirs()
    auth_path = AUTH_DIR / f"{profile}.json"
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(auth_path))
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        if profile == "xiaohongshu":
            text = await _extract_xhs(page)
        else:
            title = await page.title() or ""
            try:
                meta = page.locator('meta[name="description"]').first
                desc = await meta.get_attribute("content") or ""
            except Exception:
                desc = ""
            text = " | ".join(p for p in [title, desc] if p)

        await context.close()
        await browser.close()
        return text.strip() or None
    except Exception:
        return None
    finally:
        await pw.stop()
