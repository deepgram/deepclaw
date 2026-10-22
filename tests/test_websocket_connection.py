import asyncio
import json

import pytest
import websockets

from deepclaw import voice_agent_server as server


class PhoneSocket:
    headers = {"host": "localhost:8000"}

    def __init__(self):
        self.messages = iter([
            {
                "event": "start",
                "streamSid": "test-stream",
                "stream_id": "test-stream",
                "start": {"call_control_id": "test-call"},
            },
            {"event": "stop"},
        ])

    async def accept(self):
        pass

    async def receive_json(self):
        return next(self.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge", [server.twilio_media_websocket, server.telnyx_media_websocket])
async def test_bridge_connects_with_authorization_header(monkeypatch, bridge):
    settings_received = asyncio.Event()
    received = {}

    async def deepgram(connection):
        headers = (
            connection.request.headers
            if hasattr(connection, "request")
            else connection.request_headers
        )
        received["authorization"] = headers.get("Authorization")
        received["settings"] = json.loads(await connection.recv())
        settings_received.set()
        await connection.wait_closed()

    async def no_prewarm(session_key):
        pass

    monkeypatch.setattr(server, "DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setattr(server, "prewarm_openclaw_session", no_prewarm)
    async with websockets.serve(deepgram, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        monkeypatch.setattr(server, "DEEPGRAM_AGENT_URL", f"ws://127.0.0.1:{port}")
        await bridge(PhoneSocket())
        await asyncio.wait_for(settings_received.wait(), timeout=1)

    assert received["authorization"] == "Token test-key"
    assert received["settings"]["type"] == "Settings"
