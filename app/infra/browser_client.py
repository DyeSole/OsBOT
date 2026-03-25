"""Playwright browser client for browsing authenticated sites."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "browser"
AUTH_DIR = DATA_DIR / "auth"


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def list_profiles() -> list[str]:
    """Return saved auth profile names (without .json suffix)."""
    _ensure_dirs()
    return sorted(p.stem for p in AUTH_DIR.glob("*.json"))


def import_cookies(profile: str, cookies: list[dict], *, url: str = "") -> str:
    """Import cookies exported from a browser into a Playwright storage state file.

    *cookies* can be in either format:
    - Netscape/EditThisCookie style: [{name, value, domain, path, ...}, ...]
    - Playwright storage state:      {cookies: [...], origins: [...]}

    If a full Playwright storage state dict is passed, it is saved as-is.
    A *url* hint is only needed when cookie entries lack a ``domain`` field.
    """
    _ensure_dirs()
    auth_path = AUTH_DIR / f"{profile}.json"

    # Detect Playwright storage state format (dict with "cookies" key)
    if isinstance(cookies, dict) and "cookies" in cookies:
        auth_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"已导入 {profile} 登录态（Playwright 格式） -> {auth_path}"

    # Convert simple cookie list to Playwright storage state
    domain_hint = urlparse(url).hostname or "" if url else ""
    pw_cookies = []
    for c in cookies:
        domain = c.get("domain", domain_hint)
        secure = c.get("secure", False)
        pw_cookies.append({
            "name": c["name"],
            "value": c["value"],
            "domain": domain,
            "path": c.get("path", "/"),
            "expires": c.get("expirationDate", c.get("expires", -1)),
            "httpOnly": c.get("httpOnly", False),
            "secure": secure,
            "sameSite": c.get("sameSite", "Lax"),
        })

    state = {"cookies": pw_cookies, "origins": []}
    auth_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已导入 {profile} 登录态（{len(pw_cookies)} 条 cookie） -> {auth_path}"


def import_cookies_from_file(profile: str, file_path: str) -> str:
    """Import cookies from a JSON file on disk.

    Accepts EditThisCookie export (list), Playwright storage state (dict),
    or Netscape cookie export (list).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return import_cookies(profile, data)


async def save_login(profile: str, url: str, *, timeout_ms: int = 120_000) -> str:
    """Open a headed browser for manual login, then save storage state.

    Returns a status message.  Must be run where a display is available
    (VNC / X11 forwarding) because the browser is *not* headless.
    """
    from playwright.async_api import async_playwright

    _ensure_dirs()
    auth_path = AUTH_DIR / f"{profile}.json"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url)
        # Wait for user to finish login manually
        await asyncio.sleep(timeout_ms / 1000)
        await context.storage_state(path=str(auth_path))
        await browser.close()

    return f"已保存 {profile} 登录态 -> {auth_path}"


async def screenshot(
    url: str,
    *,
    profile: str | None = None,
    full_page: bool = False,
    wait_ms: int = 3000,
) -> bytes:
    """Take a screenshot of *url*, optionally with a saved auth profile.

    Returns PNG bytes.
    """
    from playwright.async_api import async_playwright

    _ensure_dirs()
    auth_path = AUTH_DIR / f"{profile}.json" if profile else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kwargs: dict = {}
        if auth_path and auth_path.exists():
            ctx_kwargs["storage_state"] = str(auth_path)
        # Mobile-ish viewport for cleaner screenshots
        ctx_kwargs["viewport"] = {"width": 430, "height": 932}
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        data = await page.screenshot(full_page=full_page)
        await browser.close()

    return data


async def get_page_text(
    url: str,
    *,
    profile: str | None = None,
    selector: str = "body",
    wait_ms: int = 3000,
) -> str:
    """Get text content from a page element.

    Returns the inner text of the matched *selector*.
    """
    from playwright.async_api import async_playwright

    _ensure_dirs()
    auth_path = AUTH_DIR / f"{profile}.json" if profile else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_kwargs: dict = {}
        if auth_path and auth_path.exists():
            ctx_kwargs["storage_state"] = str(auth_path)
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        text = await page.inner_text(selector)
        await browser.close()

    return text[:4000]  # Truncate to avoid flooding
