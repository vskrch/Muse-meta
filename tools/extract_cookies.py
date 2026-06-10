"""Extract Meta AI cookies from your logged-in browser.

Run this script to open a real browser window to meta.ai,
extract all cookies (including auth cookies), and save them
so the proxy server can use them.

Usage:
    python3 tools/extract_cookies.py
"""

import asyncio
import json

from playwright.async_api import async_playwright

from _paths import cookie_file

COOKIE_FILE = cookie_file()
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _extract_value(text: str, start_str: str, end_str: str) -> str:
    """Extract substring between markers."""
    start = text.find(start_str) + len(start_str)
    if start < len(start_str):
        return ""
    end = text.find(end_str, start)
    if end == -1:
        return ""
    return text[start:end]


async def main() -> None:
    """Open headed browser, extract cookies, save to disk."""
    print("=" * 60)
    print("Meta AI Cookie Extractor")
    print("=" * 60)
    print()
    print("A browser window will open at https://www.meta.ai/")
    print("If you're not logged in, log in now.")
    print("Once you're on the chat page, press ENTER in this terminal.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        page = await context.new_page()

        # Mask automation
        await page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            window.chrome = { runtime: {} };
            """
        )

        await page.goto("https://www.meta.ai/", wait_until="networkidle")
        print("Browser opened. Please log in if needed.")
        print("Press ENTER once you're logged in and on the chat page...")
        input()

        # Wait a moment for any lazy-loaded content
        await page.wait_for_timeout(2000)

        # Extract cookies from browser context
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        # Also parse HTML for embedded tokens
        text = await page.content()
        html_tokens = {
            "lsd": _extract_value(text, '"LSD",[],{"token":"', '"}'),
            "fb_dtsg": _extract_value(text, 'DTSGInitData",[],{"token":"', '"'),
        }
        for key, value in html_tokens.items():
            if value and key not in cookie_dict:
                cookie_dict[key] = value

        print()
        print("Cookies extracted:")
        preview_len = 30
        for name in sorted(cookie_dict.keys()):
            value = cookie_dict[name]
            preview = value[:preview_len] + "..." if len(value) > preview_len else value
            print(f"  {name}: {preview}")

        # Check for auth
        is_authed = "c_user" in cookie_dict or "abra_sess" in cookie_dict
        print(f"\nAuthenticated: {is_authed}")

        # Save to file
        payload = {
            "cookies": cookie_dict,
            "is_authenticated": is_authed,
            "created_at": __import__("time").time(),
        }
        COOKIE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nSaved to: {COOKIE_FILE.absolute()}")

        await browser.close()
        print("Done! You can now use the proxy server.")


if __name__ == "__main__":
    asyncio.run(main())
