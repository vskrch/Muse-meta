"""Deep diagnostic: test GraphQL endpoint directly with cookies."""
import asyncio
import json
import re
import sys

sys.path.insert(0, "src")

from muse_meta.config import Settings
from muse_meta.services.muse_client import MuseClient, META_AI_GRAPHQL, META_AI_BASE, USER_AGENT


async def main():
    settings = Settings()
    client = MuseClient(settings)

    http = await client._get_http()
    cookie_header = client._get_cookie_header()

    print(f"Cookie header: {cookie_header[:80]}...")

    # Test 1: Try GraphQL endpoint directly with just cookies
    print("\n=== Test 1: GraphQL endpoint with cookies only ===")
    headers = {
        "cookie": cookie_header,
        "content-type": "application/json",
        "origin": META_AI_BASE,
        "referer": f"{META_AI_BASE}/",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": USER_AGENT,
    }

    payload = {
        "doc_id": "ac0bad4b9787a393e160fb39f43404c1",
        "variables": {
            "conversationId": "test-conv-id",
            "content": "hello",
            "userMessageId": "test-msg-id",
            "assistantMessageId": "test-asst-id",
            "userUniqueMessageId": "1234567890123456789",
            "turnId": "test-turn-id",
            "mode": "create",
            "isNewConversation": True,
            "clientTimezone": "America/New_York",
            "entryPoint": "KADABRA__UNKNOWN",
            "promptSessionId": "test-session-id",
            "userAgent": USER_AGENT,
            "currentBranchPath": "0",
            "promptEditType": "new_message",
            "userLocale": "en-US",
            "attachments": None,
            "attachmentsV2": None,
            "mentions": None,
            "rewriteOptions": None,
            "imagineOperationRequest": None,
        },
    }

    try:
        resp = await http.post(META_AI_GRAPHQL, headers=headers, json=payload)
        print(f"Status: {resp.status_code}")
        print(f"Response headers: {dict(resp.headers)}")
        text = resp.text
        print(f"Response length: {len(text)}")
        print(f"Response body (first 2000 chars): {text[:2000]}")
    except Exception as e:
        print(f"Failed: {e}")

    # Test 2: Try with Authorization header too
    print("\n=== Test 2: GraphQL with empty OAuth token ===")
    headers2 = headers.copy()
    headers2["authorization"] = "OAuth "  # empty token

    try:
        resp = await http.post(META_AI_GRAPHQL, headers=headers2, json=payload)
        print(f"Status: {resp.status_code}")
        text = resp.text
        print(f"Response length: {len(text)}")
        print(f"Response body (first 2000 chars): {text[:2000]}")
    except Exception as e:
        print(f"Failed: {e}")

    # Test 3: Try the challenge token approach
    print("\n=== Test 3: Solve challenge then retry ===")
    # First, try to get the challenge token
    try:
        resp = await http.get(META_AI_BASE, headers={
            "cookie": cookie_header,
            "user-agent": USER_AGENT,
        })
        print(f"Homepage status: {resp.status_code}")
        text = resp.text
        # Look for challenge URL
        challenge_match = re.search(r"fetch\('([^']+)'", text)
        if challenge_match:
            challenge_url = challenge_match.group(1)
            print(f"Found challenge URL: {challenge_url}")
            # Solve the challenge
            try:
                challenge_resp = await http.post(f"{META_AI_BASE}{challenge_url}", headers={
                    "cookie": cookie_header,
                    "user-agent": USER_AGENT,
                })
                print(f"Challenge response status: {challenge_resp.status_code}")
                print(f"Challenge response headers: {dict(challenge_resp.headers)}")
                # Check for new cookies
                print(f"Cookies after challenge: {dict(http.cookies)}")
            except Exception as e:
                print(f"Challenge failed: {e}")
        else:
            print("No challenge URL found in response")
            print(f"Full response:\n{text}")
    except Exception as e:
        print(f"Homepage request failed: {e}")

    # Test 4: Check what cookies meta.ai sets
    print("\n=== Test 4: Cookie jar state ===")
    print(f"Client cookies: {dict(http.cookies)}")

    await client.close()


asyncio.run(main())
