"""Test corrected payload without rewriteOptions."""
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

    # Test the corrected payload (without rewriteOptions)
    headers = {
        "cookie": cookie_header,
        "content-type": "application/json",
        "origin": META_AI_BASE,
        "referer": f"{META_AI_BASE}/",
        "accept": "text/event-stream, application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": USER_AGENT,
    }

    payload = {
        "doc_id": "ac0bad4b9787a393e160fb39f43404c1",
        "variables": {
            "conversationId": "test-conv-12345",
            "content": "What is 2+2?",
            "userMessageId": "test-user-msg-1",
            "assistantMessageId": "test-asst-msg-1",
            "userUniqueMessageId": "1234567890123456789",
            "turnId": "test-turn-1",
            "mode": "create",
            "isNewConversation": True,
            "clientTimezone": "America/New_York",
            "entryPoint": "KADABRA__UNKNOWN",
            "promptSessionId": "test-session-1",
            "userAgent": USER_AGENT,
            "currentBranchPath": "0",
            "promptEditType": "new_message",
            "userLocale": "en-US",
            "attachments": None,
            "attachmentsV2": None,
            "mentions": None,
            "imagineOperationRequest": None,
            # REMOVED: rewriteOptions
        },
    }

    print("=== Test with corrected payload (no rewriteOptions) ===")
    try:
        resp = await http.post(META_AI_GRAPHQL, headers=headers, json=payload)
        print(f"Status: {resp.status_code}")
        text = resp.text
        print(f"Response length: {len(text)}")
        
        # Check if it's SSE
        has_data = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                has_data = True
                data = stripped[5:].strip()
                if data and data not in ("[DONE]", "null"):
                    try:
                        event = json.loads(data)
                        print(f"Event: {json.dumps(event, indent=2)[:500]}")
                    except json.JSONDecodeError:
                        print(f"Raw data: {data[:300]}")
        
        if not has_data:
            print(f"Response (first 2000 chars):\n{text[:2000]}")
        else:
            print(f"Total lines: {len(text.splitlines())}")
    except Exception as e:
        print(f"Failed: {e}")
        import traceback
        traceback.print_exc()

    await client.close()


asyncio.run(main())
