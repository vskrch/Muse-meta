"""Extract Meta AI cookies using requests-html (Pyppeteer backend).

This uses the same rendering engine as the working meta-ai-api library.

Usage:
    python3 tools/extract_via_requests_html.py
"""

import json

from requests_html import HTMLSession

from _paths import cookie_file

COOKIE_FILE = cookie_file()


def _extract_value(text: str, start_str: str, end_str: str) -> str:
    """Extract substring between markers."""
    start = text.find(start_str) + len(start_str)
    if start < len(start_str):
        return ""
    end = text.find(end_str, start)
    if end == -1:
        return ""
    return text[start:end]


def main() -> None:
    """Render meta.ai with JS and extract tokens."""
    print("=" * 60)
    print("Meta AI Cookie Extractor (requests-html)")
    print("=" * 60)
    print()

    session = HTMLSession()
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "accept-language": "en-US,en;q=0.9",
    }

    print("Fetching https://www.meta.ai/ with JavaScript rendering...")
    print("(This may take 10-30 seconds for first run)")
    resp = session.get("https://www.meta.ai/", headers=headers)
    resp.html.render(timeout=30, sleep=3)

    text = resp.html.html
    print(f"Rendered HTML length: {len(text)}")

    # Extract tokens
    cookies = {
        "_js_datr": _extract_value(text, '_js_datr":{"value":"', '",'),
        "datr": _extract_value(text, 'datr":{"value":"', '",'),
        "lsd": _extract_value(text, '"LSD",[],{"token":"', '"}'),
        "fb_dtsg": _extract_value(text, 'DTSGInitData",[],{"token":"', '"'),
        "abra_csrf": _extract_value(text, 'abra_csrf":{"value":"', '",'),
    }

    # Filter empty
    cookies = {k: v for k, v in cookies.items() if v}

    # Merge browser cookies
    for cookie in session.cookies:
        if cookie.name not in cookies:
            cookies[cookie.name] = cookie.value

    print()
    print("Cookies extracted:")
    preview_len = 30
    for name in sorted(cookies.keys()):
        value = cookies[name]
        preview = (
            value[:preview_len] + "..."
            if len(value) > preview_len
            else value
        )
        print(f"  {name}: {preview}")

    has_lsd = "lsd" in cookies
    print(f"\nHas LSD token: {has_lsd}")

    if not has_lsd:
        print("\nWARNING: No LSD token found.")
        print("Meta AI may have served a challenge page.")

    payload = {
        "cookies": cookies,
        "is_authenticated": "c_user" in cookies or "abra_sess" in cookies,
        "created_at": __import__("time").time(),
    }
    COOKIE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved to: {COOKIE_FILE.absolute()}")


if __name__ == "__main__":
    main()
