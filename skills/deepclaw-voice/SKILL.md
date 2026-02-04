---
name: deepclaw-voice
description: Set up phone calling to OpenClaw using Deepgram Voice Agent API
requires:
  bins:
    - python3
    - ngrok
---

# deepclaw Voice Setup

Use this skill when the user wants to:
- Call you on the phone
- Set up voice calling
- Talk to OpenClaw via phone
- "I want to call you"

## What This Sets Up

Phone calls to OpenClaw using:
- **Deepgram Voice Agent API** - STT, TTS, turn-taking, barge-in
- **Twilio** - Phone number routing
- **OpenClaw** - Your AI (via chat completions proxy)

## Setup Process

Walk the user through each step. Create files as needed.

### Step 1: Check Prerequisites

```bash
python3 --version  # Need 3.10+
ngrok --version
```

If ngrok missing: `brew install ngrok` (macOS) or https://ngrok.com/download

### Step 2: Deepgram Account

1. Go to https://console.deepgram.com/
2. Sign up (free $200 credit)
3. **API Keys** → **Create API Key** → Name: "deepclaw", Full Access
4. Copy key immediately

Ask: "What's your Deepgram API key?"

### Step 3: Twilio Account

1. Go to https://www.twilio.com/ and sign up
2. Copy **Account SID** and **Auth Token** from dashboard
3. **Phone Numbers** → **Buy a number** with Voice (~$1/month)

Ask: "What's your Twilio phone number, Account SID, and Auth Token?"

### Step 4: Create Project

Create directory `~/deepclaw` with these files:

**~/deepclaw/server.py:**
```python
"""
deepclaw - Call your OpenClaw over the phone.
Uses Deepgram Voice Agent API with OpenClaw as custom LLM.
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, StreamingResponse
import websockets
import httpx

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
PROXY_SECRET = os.getenv("PROXY_SECRET", secrets.token_hex(16))
DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

app = FastAPI(title="deepclaw-voice-agent")


def strip_markdown(text: str) -> str:
    """Strip markdown formatting for voice output."""
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', text)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    auth_header = request.headers.get("x-proxy-secret", "")
    if auth_header != PROXY_SECRET:
        logger.warning("Unauthorized proxy request")
        return Response(content="Unauthorized", status_code=401)

    body = await request.json()
    body["model"] = "claude-haiku-4-5"
    stream = body.get("stream", False)
    logger.info(f"Proxying - stream={stream}, messages={len(body.get('messages', []))}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
    }

    async def stream_response():
        chunk_count = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions", json=body, headers=headers) as response:
                async for chunk in response.aiter_text():
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info("First chunk from OpenClaw")
                    for line in chunk.split('\n'):
                        if line.startswith('data: ') and line != 'data: [DONE]':
                            try:
                                data = json.loads(line[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta and delta['content']:
                                        delta['content'] = strip_markdown(delta['content'])
                                yield f"data: {json.dumps(data)}\n\n"
                            except json.JSONDecodeError:
                                yield f"{line}\n"
                        elif line.strip():
                            yield f"{line}\n"
                logger.info(f"Stream complete: {chunk_count} chunks")

    if stream:
        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions", json=body, headers=headers)
            return Response(content=response.content, status_code=response.status_code, media_type="application/json")


def get_agent_config(public_url: str) -> dict:
    return {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "mulaw", "sample_rate": 8000},
            "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"},
        },
        "agent": {
            "language": "en",
            "listen": {"provider": {"type": "deepgram", "model": "flux-general-en"}},
            "think": {
                "provider": {"type": "open_ai", "model": "gpt-4o-mini"},
                "endpoint": {"url": f"{public_url}/v1/chat/completions", "headers": {"x-proxy-secret": PROXY_SECRET}},
                "prompt": "You are a helpful voice assistant on a phone call. Keep responses concise and conversational (1-3 sentences). Never use markdown, bullet points, numbered lists, or emojis - your responses will be spoken aloud.",
            },
            "speak": {"provider": {"type": "deepgram", "model": "aura-2-thalia-en"}},
            "greeting": "Hello! How can I help you?",
        },
    }


TWIML = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Connect><Stream url="wss://{host}/twilio/media" /></Connect></Response>"""


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    host = request.headers.get("host", "localhost:8000")
    logger.info(f"Incoming call, connecting to wss://{host}/twilio/media")
    return Response(content=TWIML.format(host=host), media_type="application/xml")


@app.websocket("/twilio/media")
async def twilio_media_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    stream_sid = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None
    audio_buffer = bytearray()
    BUFFER_SIZE = 20 * 160

    async def send_to_deepgram():
        nonlocal audio_buffer
        while True:
            if len(audio_buffer) >= BUFFER_SIZE and deepgram_ws:
                chunk = bytes(audio_buffer[:BUFFER_SIZE])
                audio_buffer = audio_buffer[BUFFER_SIZE:]
                try:
                    await deepgram_ws.send(chunk)
                except Exception as e:
                    logger.error(f"Error sending to Deepgram: {e}")
                    break
            await asyncio.sleep(0.01)

    async def receive_from_deepgram():
        nonlocal stream_sid
        while True:
            try:
                message = await deepgram_ws.recv()
                if isinstance(message, bytes):
                    if stream_sid:
                        await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": base64.b64encode(message).decode()}})
                else:
                    event = json.loads(message)
                    event_type = event.get("type", "")
                    if event_type == "Welcome":
                        logger.info("Connected to Deepgram Voice Agent")
                    elif event_type == "SettingsApplied":
                        logger.info("Agent settings applied")
                    elif event_type == "UserStartedSpeaking":
                        if stream_sid:
                            await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                    elif event_type == "ConversationText":
                        logger.info(f"{event.get('role', '').capitalize()}: {event.get('content', '')}")
                    elif event_type == "Error":
                        logger.error(f"Deepgram error: {event}")
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Deepgram connection closed: code={e.code}, reason={e.reason}")
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                break

    try:
        deepgram_ws = await websockets.connect(DEEPGRAM_AGENT_URL, additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"})
        logger.info("Connected to Deepgram Voice Agent API")

        while True:
            message = await websocket.receive_json()
            event = message.get("event")
            if event == "connected":
                logger.info("Twilio media stream connected")
            elif event == "start":
                stream_sid = message.get("streamSid")
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"
                logger.info(f"Stream started: {stream_sid}")
                logger.info(f"LLM endpoint URL: {public_url}/v1/chat/completions")
                config = get_agent_config(public_url)
                await deepgram_ws.send(json.dumps(config))
                logger.info("Sent agent config to Deepgram")
                sender_task = asyncio.create_task(send_to_deepgram())
                receiver_task = asyncio.create_task(receive_from_deepgram())
                break

        while True:
            message = await websocket.receive_json()
            event = message.get("event")
            if event == "media":
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    audio_buffer.extend(base64.b64decode(payload))
            elif event == "stop":
                logger.info("Stream stopped")
                break

    except WebSocketDisconnect:
        logger.info("Twilio disconnected")
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        if sender_task:
            sender_task.cancel()
        if receiver_task:
            receiver_task.cancel()
        if deepgram_ws:
            await deepgram_ws.close()
        logger.info("Cleanup complete")


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    if not DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY not set")
        exit(1)
    if not OPENCLAW_GATEWAY_TOKEN:
        logger.error("OPENCLAW_GATEWAY_TOKEN not set")
        exit(1)
    logger.info(f"Starting on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
```

**~/deepclaw/requirements.txt:**
```
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
websockets>=12.0
httpx>=0.26.0
python-dotenv>=1.0.0
```

**~/deepclaw/.env:** (use their actual values)
```
DEEPGRAM_API_KEY=<their_deepgram_key>
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=<generate_one>
```

Generate a token for them:
```bash
openssl rand -hex 32
```

### Step 5: Configure OpenClaw Gateway

Add to user's `~/.openclaw/openclaw.json`:
```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    },
    "auth": {
      "mode": "token",
      "token": "<same OPENCLAW_GATEWAY_TOKEN>"
    }
  }
}
```

Tell them to restart OpenClaw gateway.

### Step 6: Install Dependencies

```bash
cd ~/deepclaw
pip install -r requirements.txt
```

### Step 7: Start ngrok

```bash
ngrok http 8000
```

Note the HTTPS URL.

### Step 8: Configure Twilio Webhook

1. https://console.twilio.com/
2. **Phone Numbers** → **Active Numbers** → Click number
3. **Voice Configuration**:
   - A Call Comes In: **Webhook**
   - URL: `https://<ngrok-url>/twilio/incoming`
   - Method: **POST**
4. Save

### Step 9: Start Server

```bash
cd ~/deepclaw
python server.py
```

### Step 10: Test

Tell them: "Call your Twilio number now!"

---

## Customizing Voice

Edit `server.py`, find `get_agent_config()`, change the `model` in `speak`:

```python
"speak": {"provider": {"type": "deepgram", "model": "aura-2-orion-en"}},
```

### Voice Options

**English:** thalia (F, default), orion (M), apollo (M), athena (F), luna (F), zeus (M), draco (M, British), pandora (F, British), hyperion (M, Australian)

**Spanish:** estrella (F, Mexican), javier (M, Mexican), alvaro (M, Spain), celeste (F, Colombian)

**German:** fabian (M), aurelia (F), lara (F)

**French:** hector (M), agathe (F)

**Italian:** cesare (M), livia (F)

**Dutch:** lars (M), daphne (F)

**Japanese:** ebisu (M), izanami (F)

Format: `aura-2-<name>-<lang>` (e.g., `aura-2-estrella-es`)

---

## Troubleshooting

**"Application error" on call:** Check server is running, ngrok URL matches Twilio webhook

**Silence:** Verify Deepgram key, check OpenClaw gateway running on 18789

**~5 second latency:** Known OpenClaw limitation - chat completions endpoint buffers

**ngrok URL changed:** Update Twilio webhook. Use fixed domain: `ngrok http 8000 --domain=yourname.ngrok-free.app`
