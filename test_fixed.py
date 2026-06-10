"""Test the fixed MuseClient."""
import asyncio
import json
import sys
import traceback

sys.path.insert(0, "src")

from muse_meta.config import Settings
from muse_meta.services.muse_client import MuseClient, META_AI_GRAPHQL, META_AI_BASE, USER_AGENT


async def main():
    settings = Settings()
    client = MuseClient(settings)

    http = await client._get_http()
    cookie_header = client._get_cookie_header()
    print(f"Session cookies: {list(client._session.cookies.keys())}")

    # Step 1: Extract access token (tests challenge solving)
    print("\n=== Step 1: Extract access token ===")
    try:
        token = await client._get_access_token()
        print(f"Token obtained: {token[:40]}...")
    except Exception as e:
        print(f"Token extraction FAILED: {e}")
        traceback.print_exc()
        await client.close()
        return

    # Step 2: Test chat completion
    print("\n=== Step 2: Chat completion ===")
    from muse_meta.models.chat import ChatCompletionRequest, ChatMessage
    req = ChatCompletionRequest(
        model="muse-spark",
        messages=[ChatMessage(role="user", content="What is 2+2? Reply with just the number.")],
    )
    try:
        resp = await client.chat_completion(req)
        print(f"Response model: {resp.model}")
        print(f"Response text: {resp.choices[0].message.content}")
        print(f"Usage: {resp.usage}")
    except Exception as e:
        print(f"Chat completion FAILED: {e}")
        traceback.print_exc()

    # Step 3: Test streaming
    print("\n=== Step 3: Streaming chat completion ===")
    req_stream = ChatCompletionRequest(
        model="muse-spark",
        messages=[ChatMessage(role="user", content="Say hello in one sentence.")],
        stream=True,
    )
    try:
        full_text = ""
        async for chunk in client.chat_completion_stream(req_stream):
            delta = chunk.choices[0].delta.content or ""
            full_text += delta
        print(f"Streamed text: {full_text}")
    except Exception as e:
        print(f"Streaming FAILED: {e}")
        traceback.print_exc()

    await client.close()
    print("\n=== All tests complete ===")


asyncio.run(main())
