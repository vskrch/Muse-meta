"""Extract Meta AI cookies using a persistent browser profile.

This script creates a persistent browser profile directory so that
your login session is preserved across runs. It then navigates to
meta.ai and extracts the lsd token and other cookies needed for
the GraphQL API.

Usage:
    python3 tools/extract_cookies_persistent.py
"""

import asyncio
import json

from playwright.async_api import async_playwright

from _paths import cookie_file, profile_dir

COOKIE_FILE = cookie_file()
PROFILE_DIR = profile_dir()
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
    """Open browser with persistent profile, extract cookies."""
    print("=" * 60)
    print("Meta AI Cookie Extractor (Persistent Profile)")
    print("=" * 60)
    print()
    print(f"Profile directory: {PROFILE_DIR}")
    print()
    print("A browser window will open.")
    print("If you're not logged in, log in to Meta AI.")
    print("Once on the chat page, press ENTER in this terminal.")
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Launch with persistent context (saves cookies, localStorage, etc.)
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
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
        print("Press ENTER once you're on the chat page...")
        input()

        # Wait for lazy content
        await page.wait_for_timeout(3000)

        # Evaluate JS to get cookies and tokens
        js_result = await page.evaluate(
            """
            () => {
                // Get all cookies
                const cookies = document.cookie.split('; ').reduce((acc, c) => {
                    const [name, ...rest] = c.split('=');
                    if (name) acc[name] = rest.join('=');
                    return acc;
                }, {});

                // Try to find lsd in page scripts
                let lsd = '';
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    const match = text.match(/"LSD",\\[],\\{"token":"([^"]+)"/);
                    if (match) {
                        lsd = match[1];
                        break;
                    }
                }

                // Try fb_dtsg
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

        # Add extracted tokens
        if lsd:
            cookie_dict["lsd"] = lsd
        if fb_dtsg:
            cookie_dict["fb_dtsg"] = fb_dtsg

        # Also get context-level cookies (includes HttpOnly)
        context_cookies = await context.cookies()
        for c in context_cookies:
            name = c.get("name", "")
            if name not in cookie_dict:
                cookie_dict[name] = c.get("value", "")

        print()
        print("Cookies extracted:")
        for name in sorted(cookie_dict.keys()):
            value = cookie_dict[name]
            preview_len = 30
            preview = value[:preview_len] + "..." if len(value) > preview_len else value
            print(f"  {name}: {preview}")

        # Check for auth
        is_authed = "c_user" in cookie_dict or "abra_sess" in cookie_dict
        has_lsd = "lsd" in cookie_dict
        print(f"\nAuthenticated: {is_authed}")
        print(f"Has LSD token: {has_lsd}")

        if not has_lsd:
            print("\nWARNING: No LSD token found. Meta AI may block API calls.")

        # Save
        payload = {
            "cookies": cookie_dict,
            "is_authenticated": is_authed,
            "created_at": __import__("time").time(),
        }
        COOKIE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nSaved to: {COOKIE_FILE.absolute()}")

        await context.close()
        print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
