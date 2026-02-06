"""
Simplified server using Deepgram Voice Agent API with OpenClaw as custom LLM.

Deepgram handles: Flux STT, Aura-2 TTS, turn-taking, barge-in
OpenClaw handles: LLM responses via /v1/chat/completions
This server: bridges Twilio <-> Deepgram Voice Agent API AND proxies LLM requests to OpenClaw
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import subprocess

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

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Voice Provider Configuration
VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "twilio").lower()

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# Telnyx Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_PUBLIC_KEY = os.getenv("TELNYX_PUBLIC_KEY", "")

# Generate a random proxy secret on startup (Deepgram will send this back to us)
PROXY_SECRET = os.getenv("PROXY_SECRET", secrets.token_hex(16))

DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

app = FastAPI(title="deepclaw-voice-agent")

# Map Deepgram caller IP → OpenClaw session key for the active call.
# Deepgram sends LLM requests from a fixed IP per call session, so we
# use the caller IP to correlate proxy requests with the right call.
_active_sessions: dict[str, str] = {}


async def prewarm_openclaw_session(session_key: str):
    """Fire a throwaway request to OpenClaw to warm the session and prompt cache.

    This creates the session file, loads skills/tools, and writes the
    Anthropic prompt cache (~15 k tokens).  Subsequent requests in the
    same session hit a warm cache and skip the cold-start penalty.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
        "X-OpenClaw-Session-Key": session_key,
    }
    body = {
        "model": "openclaw/voice",
        "stream": True,
        "messages": [{"role": "user", "content": "warmup"}],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as response:
                # Drain the stream so the session completes
                async for _ in response.aiter_bytes():
                    pass
        logger.info("OpenClaw session pre-warmed: %s", session_key)
    except Exception as exc:
        logger.warning("Pre-warm failed (non-fatal): %s", exc)


def strip_markdown(text: str) -> str:
    """Strip markdown formatting for voice output."""
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove bold/italic
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points and numbered lists
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Remove links, keep text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove images
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', text)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Remove blockquotes
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    # Remove common emojis (basic set)
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF]+', '', text)
    # Collapse multiple newlines into spaces for voice
    text = re.sub(r'\n+', ' ', text)
    return text


# ============================================================================
# LLM Proxy - Deepgram calls this, we forward to local OpenClaw
# ============================================================================

@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """
    Proxy LLM requests from Deepgram Voice Agent to local OpenClaw.
    This eliminates the need for a second ngrok tunnel.
    """
    logger.info("LLM proxy request received")

    body = await request.json()

    # Route to the 'voice' agent (configured with claude-haiku-4-5)
    body["model"] = "openclaw/voice"

    stream = body.get("stream", False)
    logger.info(f"Proxying chat completion - stream={stream}, messages={len(body.get('messages', []))}")

    # Look up the stable session key for this call.
    # Deepgram's cloud IPs aren't known in advance, so we use
    # a catch-all that maps to the most recent active call.
    session_key = _active_sessions.get("_current")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
    }
    if session_key:
        headers["X-OpenClaw-Session-Key"] = session_key
        logger.info(f"Using session key: {session_key}")

    async def stream_response():
        """Stream the response from OpenClaw, stripping markdown for voice."""
        chunk_count = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as response:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info("First chunk received from OpenClaw")

                    if line.startswith('data: ') and line != 'data: [DONE]':
                        try:
                            data = json.loads(line[6:])
                            if 'choices' in data and data['choices']:
                                delta = data['choices'][0].get('delta', {})
                                if 'content' in delta and delta['content']:
                                    delta['content'] = strip_markdown(delta['content'])
                            yield f"data: {json.dumps(data)}\n\n"
                        except json.JSONDecodeError as exc:
                            logger.warning("Malformed SSE data: %s", exc)
                            yield f"{line}\n\n"
                    elif line.strip():
                        yield f"{line}\n\n"

                logger.info(f"Stream complete: {chunk_count} chunks")

    if stream:
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
        )
    else:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type="application/json",
            )


# ============================================================================
# Agent Configuration
# ============================================================================

def get_agent_config(public_url: str) -> dict:
    """Build Deepgram Voice Agent configuration with OpenClaw as custom LLM."""

    # Point Deepgram to OUR proxy endpoint (same ngrok URL)
    llm_url = f"{public_url}/v1/chat/completions"

    return {
        "type": "Settings",
        "audio": {
            "input": {
                "encoding": "mulaw",
                "sample_rate": 8000,
            },
            "output": {
                "encoding": "mulaw",
                "sample_rate": 8000,
                "container": "none",
            },
        },
        "agent": {
            "language": "en",
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": "flux-general-en",
                },
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": "gpt-4o-mini",
                },
                "endpoint": {
                    "url": llm_url,
                },
                "prompt": "You are a helpful voice assistant on a phone call. Keep responses concise and conversational (1-3 sentences). Never use markdown, bullet points, numbered lists, or emojis - your responses will be spoken aloud.",
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": "aura-2-thalia-en",
                },
            },
            "greeting": "Hello! How can I help you?",
        },
    }


# ============================================================================
# Twilio Webhook & Media Stream
# ============================================================================

TWIML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/twilio/media" />
    </Connect>
</Response>"""


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Handle incoming Twilio call - returns TwiML to start media stream.

    Note: For production, add Twilio signature validation:
    https://www.twilio.com/docs/usage/security#validating-requests
    """
    host = request.headers.get("host", "localhost:8000")
    twiml = TWIML_TEMPLATE.format(host=host)
    logger.info(f"Incoming call, connecting to wss://{host}/twilio/media")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/media")
async def twilio_media_websocket(websocket: WebSocket):
    """Bridge Twilio media stream to Deepgram Voice Agent API."""
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    stream_sid: str | None = None
    session_key: str | None = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None
    prewarm_task = None

    # Audio buffer for batching
    audio_buffer = bytearray()
    BUFFER_SIZE = 20 * 160  # 20 messages * 160 bytes = 0.4 seconds at 8kHz mulaw

    async def send_to_deepgram():
        """Forward buffered audio from Twilio to Deepgram."""
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
        """Receive audio/events from Deepgram and send to Twilio."""
        nonlocal stream_sid
        while True:
            try:
                message = await deepgram_ws.recv()

                # Binary = audio data
                if isinstance(message, bytes):
                    if stream_sid:
                        payload = base64.b64encode(message).decode("utf-8")
                        media_msg = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }
                        await websocket.send_json(media_msg)

                # Text = JSON event
                else:
                    event = json.loads(message)
                    event_type = event.get("type", "")

                    if event_type == "Welcome":
                        logger.info("Connected to Deepgram Voice Agent")
                    elif event_type == "SettingsApplied":
                        logger.info("Agent settings applied")
                    elif event_type == "UserStartedSpeaking":
                        logger.debug("User started speaking")
                        # Clear any queued audio (barge-in)
                        if stream_sid:
                            await websocket.send_json({
                                "event": "clear",
                                "streamSid": stream_sid,
                            })
                    elif event_type == "AgentStartedSpeaking":
                        logger.debug("Agent started speaking")
                    elif event_type == "ConversationText":
                        role = event.get("role", "")
                        content = event.get("content", "")
                        logger.info(f"{role.capitalize()}: {content}")
                    elif event_type == "Error":
                        logger.error(f"Deepgram error: {event}")

            except websockets.exceptions.ConnectionClosed:
                logger.info("Deepgram connection closed")
                break
            except Exception as e:
                logger.error(f"Error receiving from Deepgram: {e}")
                break

    try:
        # Connect to Deepgram Voice Agent API
        deepgram_ws = await websockets.connect(
            DEEPGRAM_AGENT_URL,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        )
        logger.info("Connected to Deepgram Voice Agent API")

        # Wait for stream to start to get the public URL
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "connected":
                logger.info("Twilio media stream connected")

            elif event == "start":
                stream_sid = message.get("streamSid")

                # Get the public URL from the websocket headers
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"

                logger.info(f"Stream started: {stream_sid}")
                logger.info(f"Public URL for LLM proxy: {public_url}")

                # Create a stable session key for this call and register
                # it so the LLM proxy can find it when Deepgram calls back.
                session_key = f"agent:voice:call:{stream_sid}"
                _active_sessions[public_url] = session_key
                # Deepgram calls us from its cloud IPs — register a
                # catch-all so any caller hitting /v1/chat/completions
                # during this call gets the right session.
                _active_sessions["_current"] = session_key
                logger.info(f"Session key: {session_key}")

                # Pre-warm the OpenClaw session in the background so the
                # prompt cache is hot by the time the user speaks.
                prewarm_task = asyncio.create_task(
                    prewarm_openclaw_session(session_key)
                )

                # Now send agent config with correct URL
                config = get_agent_config(public_url)
                await deepgram_ws.send(json.dumps(config))
                logger.info("Sent agent config")

                # Start background tasks
                sender_task = asyncio.create_task(send_to_deepgram())
                receiver_task = asyncio.create_task(receive_from_deepgram())
                break

        # Continue processing Twilio messages
        while True:
            message = await websocket.receive_json()
            event = message.get("event")

            if event == "media":
                # Decode and buffer audio
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    audio_data = base64.b64decode(payload)
                    audio_buffer.extend(audio_data)

            elif event == "stop":
                logger.info("Stream stopped")
                break

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in media WebSocket: {e}")
    finally:
        # Cleanup
        if sender_task:
            sender_task.cancel()
        if receiver_task:
            receiver_task.cancel()
        if prewarm_task:
            prewarm_task.cancel()
        if deepgram_ws:
            await deepgram_ws.close()
        # Remove session mapping
        if session_key:
            _active_sessions.pop("_current", None)
            for k, v in list(_active_sessions.items()):
                if v == session_key:
                    del _active_sessions[k]
        logger.info("Cleanup complete")


# ============================================================================
# Telnyx Webhook & Media Stream
# ============================================================================

@app.post("/telnyx/webhook")
async def telnyx_webhook(request: Request):
    """Handle Telnyx webhook events - incoming calls and call control."""
    body = await request.json()
    event_type = body.get("data", {}).get("event_type", "")
    
    logger.info(f"Telnyx webhook received: {event_type}")
    
    if event_type == "call.initiated":
        # Incoming call - answer and start media streaming
        call_control_id = body["data"]["payload"]["call_control_id"]
        
        # Get the public URL from the request headers
        host = request.headers.get("host", "localhost:8000")
        stream_url = f"wss://{host}/telnyx/media"
        
        # Answer the call with media streaming
        answer_data = {
            "stream_url": stream_url,
            "stream_track": "both_tracks"
        }
        
        headers = {
            "Authorization": f"Bearer {TELNYX_API_KEY}",
            "Content-Type": "application/json"
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer",
                    json=answer_data,
                    headers=headers
                )
                logger.info(f"Answered Telnyx call: {response.status_code}")
        except Exception as e:
            logger.error(f"Error answering Telnyx call: {e}")
    
    elif event_type == "call.answered":
        logger.info("Telnyx call answered")
    elif event_type == "call.hangup":
        logger.info("Telnyx call ended")
    elif event_type == "streaming.started":
        logger.info("Telnyx media streaming started")
    elif event_type == "streaming.stopped":
        logger.info("Telnyx media streaming stopped")
    
    return {"status": "ok"}


@app.websocket("/telnyx/media")
async def telnyx_media_websocket(websocket: WebSocket):
    """Bridge Telnyx media stream to Deepgram Voice Agent API."""
    await websocket.accept()
    logger.info("Telnyx WebSocket connected")
    
    call_control_id: str | None = None
    stream_id: str | None = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None
    
    # Audio buffer for batching
    audio_buffer = bytearray()
    BUFFER_SIZE = 20 * 160  # 20 messages * 160 bytes = 0.4 seconds at 8kHz PCMU
    
    async def send_to_deepgram():
        """Forward buffered audio from Telnyx to Deepgram."""
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
        """Receive audio/events from Deepgram and send to Telnyx."""
        nonlocal call_control_id
        while True:
            try:
                message = await deepgram_ws.recv()
                
                # Binary = audio data
                if isinstance(message, bytes):
                    if call_control_id:
                        payload = base64.b64encode(message).decode("utf-8")
                        media_msg = {
                            "event": "media",
                            "media": {"payload": payload}
                        }
                        await websocket.send_json(media_msg)
                
                # Text = JSON event
                else:
                    event = json.loads(message)
                    event_type = event.get("type", "")
                    
                    if event_type == "Welcome":
                        logger.info("Connected to Deepgram Voice Agent")
                    elif event_type == "SettingsApplied":
                        logger.info("Agent settings applied")
                    elif event_type == "UserStartedSpeaking":
                        logger.debug("User started speaking")
                        # Clear any queued audio (barge-in)
                        if call_control_id:
                            await websocket.send_json({"event": "clear"})
                    elif event_type == "AgentStartedSpeaking":
                        logger.debug("Agent started speaking")
                    elif event_type == "ConversationText":
                        role = event.get("role", "")
                        content = event.get("content", "")
                        logger.info(f"{role.capitalize()}: {content}")
                    elif event_type == "Error":
                        logger.error(f"Deepgram error: {event}")
            
            except websockets.exceptions.ConnectionClosed:
                logger.info("Deepgram connection closed")
                break
            except Exception as e:
                logger.error(f"Error receiving from Deepgram: {e}")
                break
    
    try:
        # Connect to Deepgram Voice Agent API
        deepgram_ws = await websockets.connect(
            DEEPGRAM_AGENT_URL,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        )
        logger.info("Connected to Deepgram Voice Agent API")
        
        # Wait for stream to start
        while True:
            message = await websocket.receive_json()
            event_type = message.get("event")
            
            if event_type == "connected":
                logger.info("Telnyx media stream connected")
            
            elif event_type == "start":
                # Extract call information from Telnyx start event
                start_data = message.get("start", {})
                call_control_id = start_data.get("call_control_id")
                stream_id = message.get("stream_id")
                
                # Get the public URL from the websocket headers
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"
                
                logger.info(f"Telnyx stream started: call_control_id={call_control_id}, stream_id={stream_id}")
                logger.info(f"Public URL for LLM proxy: {public_url}")
                
                # Send agent config with correct URL
                config = get_agent_config(public_url)
                await deepgram_ws.send(json.dumps(config))
                logger.info("Sent agent config")
                
                # Start background tasks
                sender_task = asyncio.create_task(send_to_deepgram())
                receiver_task = asyncio.create_task(receive_from_deepgram())
                break
        
        # Continue processing Telnyx messages
        while True:
            message = await websocket.receive_json()
            event_type = message.get("event")
            
            if event_type == "media":
                # Decode and buffer audio from Telnyx
                media_data = message.get("media", {})
                payload = media_data.get("payload", "")
                if payload:
                    audio_data = base64.b64decode(payload)
                    audio_buffer.extend(audio_data)
            
            elif event_type == "stop":
                logger.info("Telnyx stream stopped")
                break
            
            elif event_type == "dtmf":
                dtmf_data = message.get("dtmf", {})
                digit = dtmf_data.get("digit", "")
                logger.info(f"DTMF received: {digit}")
            
            elif event_type == "error":
                error_data = message.get("payload", {})
                logger.error(f"Telnyx error: {error_data}")
    
    except WebSocketDisconnect:
        logger.info("Telnyx WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in Telnyx media WebSocket: {e}")
    finally:
        # Cleanup
        if sender_task:
            sender_task.cancel()
        if receiver_task:
            receiver_task.cancel()
        if deepgram_ws:
            await deepgram_ws.close()
        logger.info("Telnyx cleanup complete")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "deepclaw-voice-agent"}


OPENCLAW_VOICE_MODEL = os.getenv(
    "OPENCLAW_VOICE_MODEL", "anthropic/claude-haiku-4-5-20251001"
)


def ensure_openclaw_voice_agent():
    """Create the 'voice' OpenClaw agent if it doesn't already exist.

    The voice agent uses a fast model (Haiku by default) to keep
    time-to-first-token low for real-time phone conversations.
    """
    openclaw = shutil.which("openclaw")
    if not openclaw:
        logger.warning(
            "openclaw CLI not found on PATH — skipping voice agent provisioning. "
            "Install OpenClaw or create the agent manually: "
            "openclaw agents add voice --model %s",
            OPENCLAW_VOICE_MODEL,
        )
        return

    # Check if the voice agent already exists
    try:
        result = subprocess.run(
            [openclaw, "agents", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if "voice" in result.stdout.split():
            logger.info("OpenClaw 'voice' agent already exists")
            return
    except Exception as exc:
        logger.warning("Could not list OpenClaw agents: %s", exc)
        return

    # Create it
    logger.info(
        "Creating OpenClaw 'voice' agent with model %s", OPENCLAW_VOICE_MODEL
    )
    try:
        workspace = os.path.join(
            os.path.expanduser("~"), ".openclaw", "workspace-voice"
        )
        subprocess.run(
            [
                openclaw, "agents", "add", "voice",
                "--model", OPENCLAW_VOICE_MODEL,
                "--workspace", workspace,
                "--non-interactive",
            ],
            capture_output=True, text=True, timeout=15, check=True,
        )
        logger.info("OpenClaw 'voice' agent created successfully")
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Failed to create OpenClaw voice agent: %s\n%s",
            exc, exc.stderr,
        )


def main():
    """Run the server."""
    import uvicorn

    # Validate required configuration
    if not DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY not set. Get one at https://console.deepgram.com/")
        return
    if not OPENCLAW_GATEWAY_TOKEN:
        logger.error("OPENCLAW_GATEWAY_TOKEN not set. Generate with: openssl rand -hex 32")
        return
    
    # Validate voice provider configuration
    if VOICE_PROVIDER == "twilio":
        if not TWILIO_ACCOUNT_SID:
            logger.error("TWILIO_ACCOUNT_SID not set for Twilio provider")
            return
        if not TWILIO_AUTH_TOKEN:
            logger.error("TWILIO_AUTH_TOKEN not set for Twilio provider")
            return
        logger.info("Using Twilio as voice provider")
    elif VOICE_PROVIDER == "telnyx":
        if not TELNYX_API_KEY:
            logger.error("TELNYX_API_KEY not set for Telnyx provider")
            return
        if not TELNYX_PUBLIC_KEY:
            logger.error("TELNYX_PUBLIC_KEY not set for Telnyx provider")
            return
        logger.info("Using Telnyx as voice provider")
    else:
        logger.error(f"Invalid VOICE_PROVIDER: {VOICE_PROVIDER}. Must be 'twilio' or 'telnyx'")
        return

    ensure_openclaw_voice_agent()

    logger.info(f"Starting deepclaw voice agent server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
