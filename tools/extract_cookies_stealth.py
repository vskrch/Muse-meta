"""Extract Meta AI cookies with maximum anti-detection.

Uses playwright-stealth, headed mode, human-like behavior,
and persistent profile to evade bot detection.

Usage:
    python3 tools/extract_cookies_stealth.py
"""

import asyncio
import contextlib
import json
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth.stealth import Stealth

COOKIE_FILE = Path(".meta_ai_cookies.json")
PROFILE_DIR = Path(".playwright_profile").absolute()


def _extract_value(text: str, start_str: str, end_str: str) -> str:
    """Extract substring between markers."""
    start = text.find(start_str) + len(start_str)
    if start < len(start_str):
        return ""
    end = text.find(end_str, start)
    if end == -1:
        return ""
    return text[start:end]


async def _human_delay(min_ms: int = 200, max_ms: int = 800) -> None:
    """Wait a random human-like duration."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def main() -> None:
    """Open stealth browser, extract cookies."""
    print("=" * 60)
    print("Meta AI Cookie Extractor (Stealth Mode)")
    print("=" * 60)
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch with persistent context for cookie persistence
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
            ],
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            reduced_motion="no-preference",
        )

        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        print("Browser opened with stealth patches.")
        print("Navigating to https://www.meta.ai/ ...")

        # Human-like navigation with random delays
        await _human_delay(300, 700)
        await page.goto("https://www.meta.ai/", wait_until="domcontentloaded")
        await _human_delay(500, 1200)

        # Scroll a bit like a human
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(100, 300))
            await _human_delay(400, 900)

        # Wait for full load
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15000)

        print()
        print("If you're not logged in, please log in now.")
        print("Press ENTER once you're on the chat page...")
        input()

        # More human-like idle time
        await _human_delay(1000, 2000)

        cookie_dict = await _extract_all_cookies(page, context)
        _print_cookie_summary(cookie_dict)

        await context.close()
        print("Done! Restart the proxy server and test with curl.")


async def _extract_all_cookies(page, context) -> dict[str, str]:
    """Extract cookies from JS, browser context, and HTML."""
    js_result = await page.evaluate(
        """
        () => {
            const cookies = document.cookie.split('; ').reduce((acc, c) => {
                const [name, ...rest] = c.split('=');
                if (name) acc[name] = rest.join('=');
                return acc;
            }, {});

            let lsd = '';
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const text = s.textContent || '';
                const match = text.match(/"LSD",\\[],\\{"token":"([^"]+)"/);
                if (match) { lsd = match[1]; break; }
            }

            let fb_dtsg = '';
            const dtsgInput = document.querySelector('input[name="fb_dtsg"]');
            if (dtsgInput) fb_dtsg = dtsgInput.value;

            return { cookies, lsd, fb_dtsg };
        }
        """
    )

    cookie_dict = js_result.get("cookies", {})
    lsd = js_result.get("lsd", "")
    fb_dtsg = js_result.get("fb_dtsg", "")

    if lsd:
        cookie_dict["lsd"] = lsd
    if fb_dtsg:
        cookie_dict["fb_dtsg"] = fb_dtsg

    # Merge context-level cookies (HttpOnly)
    context_cookies = await context.cookies()
    for c in context_cookies:
        name = c.get("name", "")
        if name not in cookie_dict:
            cookie_dict[name] = c.get("value", "")

    # Parse HTML as final fallback
    text = await page.content()
    html_tokens = {
        "lsd": _extract_value(text, '"LSD",[],{"token":"', '"}'),
        "fb_dtsg": _extract_value(text, 'DTSGInitData",[],{"token":"', '"'),
        "_js_datr": _extract_value(text, '_js_datr":{"value":"', '",'),
        "datr": _extract_value(text, 'datr":{"value":"', '",'),
        "abra_csrf": _extract_value(text, 'abra_csrf":{"value":"', '",'),
    }
    for key, value in html_tokens.items():
        if value and key not in cookie_dict:
            cookie_dict[key] = value

    return cookie_dict


def _print_cookie_summary(cookie_dict: dict[str, str]) -> None:
    """Print extracted cookies and save to file."""
    print()
    print("Cookies extracted:")
    for name in sorted(cookie_dict.keys()):
        value = cookie_dict[name]
        preview_len = 30
        preview = (
            value[:preview_len] + "..."
            if len(value) > preview_len
            else value
        )
        print(f"  {name}: {preview}")

    is_authed = "c_user" in cookie_dict or "abra_sess" in cookie_dict
    has_lsd = "lsd" in cookie_dict
    print(f"\nAuthenticated: {is_authed}")
    print(f"Has LSD token: {has_lsd}")

    if not has_lsd:
        print("\nWARNING: No LSD token. Meta AI may still block API calls.")

    payload = {
        "cookies": cookie_dict,
        "is_authenticated": is_authed,
        "created_at": time.time(),
    }
    COOKIE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved to: {COOKIE_FILE.absolute()}")


if __name__ == "__main__":
    asyncio.run(main())
