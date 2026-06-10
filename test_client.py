"""Diagnostic script to test MuseClient."""
import asyncio
import json
import sys
import traceback

sys.path.insert(0, "src")

from muse_meta.config import Settings
from muse_meta.services.muse_client import MuseClient, _COOKIE_FILE


async def main():
    settings = Settings()
    print(f"Cookies from env: datr={bool(settings.meta_ai_datr)}, ecto={bool(settings.meta_ai_ecto_1_sess)}, access_token={bool(settings.meta_ai_access_token)}")
    print(f"Cookie file exists: {_COOKIE_FILE.exists()}")
    if _COOKIE_FILE.exists():
        print(f"Cookie file content: {_COOKIE_FILE.read_text()}")

    client = MuseClient(settings)
    print(f"Session cookies: {client._session.cookies}")
    print(f"Session access_token: '{client._session.access_token}'")

    # Step 1: Try to extract access token
    print("\n--- Step 1: Extracting access token ---")
    try:
        token = await client._extract_access_token()
        print(f"Got token: {token[:30]}..." if len(token) > 30 else f"Got token: {token}")
    except Exception as e:
        print(f"Token extraction failed: {e}")
        traceback.print_exc()

        # Try the raw request to see what meta.ai returns
        print("\n--- Raw request to meta.ai ---")
        http = await client._get_http()
        try:
            resp = await http.get("https://www.meta.ai", headers={
                "cookie": client._get_cookie_header(),
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            })
            print(f"Status: {resp.status_code}")
            text = resp.text
            print(f"Response length: {len(text)}")
            # Check for ecto1 pattern
            import re
            matches = re.findall(r"ecto1:[a-zA-Z0-9_-]+", text)
            print(f"ecto1 matches: {matches}")
            # Check for other token patterns
            bearer_matches = re.findall(r'"accessToken":"([^"]+)"', text)
            print(f"accessToken matches: {bearer_matches}")
            # Print first 3000 chars
            print(f"\nFirst 3000 chars:\n{text[:3000]}")
        except Exception as e2:
            print(f"Raw request also failed: {e2}")
            traceback.print_exc()

    # Step 2: If token works, try a chat completion
    if client._session.access_token:
        print("\n--- Step 2: Testing chat completion ---")
        from muse_meta.models.chat import ChatCompletionRequest, ChatMessage
        req = ChatCompletionRequest(
            model="muse-spark",
            messages=[ChatMessage(role="user", content="Hello, what is 2+2?")],
        )
        try:
            resp = await client.chat_completion(req)
            print(f"Response: {resp}")
        except Exception as e:
            print(f"Chat completion failed: {e}")
            traceback.print_exc()

    await client.close()


asyncio.run(main())
