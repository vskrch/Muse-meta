"""Network traffic analyzer for Meta AI chat.

Run this script to capture the API endpoints, headers, and payloads
used by https://meta.ai/ during chat interactions.

Usage:
    python tools/analyze_meta_ai.py

The script will:
1. Launch a headed browser
2. Prompt you to log in manually
3. Start recording all network requests
4. Prompt you to send a chat message
5. Save captured requests to meta_ai_requests.jsonl
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import Page, async_playwright

CAPTURE_FILE = Path("meta_ai_requests.jsonl")


def _should_capture(url: str) -> bool:
    """Filter for likely Meta AI API requests."""
    interesting = [
        "meta.ai",
        "facebook.com",
        "fbcdn.net",
        "instagram.com",
    ]
    return any(domain in url for domain in interesting)


def _setup_route_handler(page: Page, captured_requests: list) -> None:
    """Attach network route interception to the page."""
    async def handle_route(route, request):
        url = request.url
        if _should_capture(url):
            try:
                post_data = request.post_data
            except Exception:
                post_data = None

            entry = {
                "method": request.method,
                "url": url,
                "headers": dict(request.headers),
                "post_data": post_data,
            }
            captured_requests.append(entry)
            print(f"[CAPTURED] {request.method} {url[:80]}")
        await route.continue_()

    page.route("**/*", handle_route)


def _setup_response_handler(page: Page, captured_requests: list) -> None:
    """Attach response listener to capture API response bodies."""
    page.on("response", lambda response: asyncio.create_task(
        _log_response(response, captured_requests)
    ))


async def _log_response(response, captured_requests: list) -> None:
    """Capture response bodies for API calls."""
    url = response.url
    if not _should_capture(url):
        return
    try:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or "graphql" in url:
            body = await response.text()
            for req in reversed(captured_requests):
                if req["url"] == url and req.get("response_body") is None:
                    req["response_body"] = body[:5000]
                    req["status"] = response.status
                    break
    except Exception:
        pass


def _print_summary(captured_requests: list) -> None:
    """Print a summary of likely API endpoints."""
    api_requests = [
        r for r in captured_requests
        if r["method"] in ("POST", "GET", "PATCH")
        and ("api" in r["url"] or "graphql" in r["url"] or "ajax" in r["url"])
    ]

    if not api_requests:
        return

    print()
    print("=" * 60)
    print("Likely API endpoints found:")
    print("=" * 60)
    for req in api_requests:
        print(f"\n{req['method']} {req['url']}")
        if req.get("post_data"):
            preview = str(req["post_data"])[:200]
            print(f"  Payload preview: {preview}")


def _save_captures(captured_requests: list) -> None:
    """Write captured requests to a JSONL file."""
    with open(CAPTURE_FILE, "w", encoding="utf-8") as f:
        for entry in captured_requests:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"\nSaved to: {CAPTURE_FILE.absolute()}")


async def main() -> None:
    """Run the network capture session."""
    print("=" * 60)
    print("Meta AI Chat API Traffic Analyzer")
    print("=" * 60)
    print("\nThis script captures network requests from meta.ai")
    print("so you can reverse-engineer the chat API.\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        captured_requests = []

        _setup_route_handler(page, captured_requests)
        _setup_response_handler(page, captured_requests)

        print("Navigating to https://meta.ai/ ...")
        await page.goto("https://meta.ai/", wait_until="networkidle")

        print("\nPlease log in manually in the browser window.")
        print("Press ENTER here once you are logged in and ready to chat...")
        input()

        print("\nNow send a chat message in the browser.")
        print(
            "Press ENTER here once you have sent a message "
            "and received a response..."
        )
        input()

        _save_captures(captured_requests)
        print(f"Captured {len(captured_requests)} requests total.")
        _print_summary(captured_requests)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
