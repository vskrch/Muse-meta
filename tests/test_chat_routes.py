"""OpenAI-compatible chat route tests."""

from fastapi.testclient import TestClient


def test_chat_completion_returns_openai_shape(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-token"},
        json={
            "model": "muse-spark",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "chatcmpl-test"
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "ok"


def test_empty_messages_are_rejected_by_schema(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-token"},
        json={"model": "muse-spark", "messages": []},
    )

    assert response.status_code == 422


def test_message_count_is_bounded(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-token"},
        json={
            "model": "muse-spark",
            "messages": [{"role": "user", "content": "hello"} for _ in range(129)],
        },
    )

    assert response.status_code == 422
