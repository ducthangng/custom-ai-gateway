"""Small helpers shared by the test files."""

import json

from fastapi.testclient import TestClient


def parse_sse_events(body: str) -> list[tuple[str, dict]]:
    """Turn a streamed (SSE) response body into a list of (event name, data)."""
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def send_chat(client: TestClient, message: str, conversation_id=None, **settings) -> list[tuple[str, dict]]:
    """Send one chat to the mock model (override with settings=...) and return its events.
    Fails the test if the HTTP status is not 200."""
    body = {
        "conversation_id": conversation_id,
        "message": message,
        "settings": {"provider": "mock", "model": "echo", **settings},
    }
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    return parse_sse_events(r.text)
