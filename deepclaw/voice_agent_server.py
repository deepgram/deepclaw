"""
Simplified server using Deepgram Voice Agent API with OpenClaw as custom LLM.

Deepgram handles: Flux STT, Aura-2 TTS, turn-taking, barge-in
OpenClaw handles: LLM responses via /v1/chat/completions
This server: bridges Twilio <-> Deepgram Voice Agent API AND proxies LLM requests to OpenClaw
"""

import asyncio
import base64
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, StreamingResponse
import websockets
import httpx

try:
    from twilio.base.exceptions import TwilioRestException
    from twilio.request_validator import RequestValidator
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None
    RequestValidator = None

    class TwilioRestException(Exception):
        """Fallback exception when twilio is not installed."""

        def __init__(self, message: str = "", code: str | None = None):
            super().__init__(message)
            self.code = code

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
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
OWNER_PHONE = os.getenv("OWNER_PHONE", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
TWILIO_VALIDATE_SIGNATURES = os.getenv("TWILIO_VALIDATE_SIGNATURES", "true")

# Telnyx Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_PUBLIC_KEY = os.getenv("TELNYX_PUBLIC_KEY", "")

# Local control API configuration (used for outbound call/text triggers)
CONTROL_API_TOKEN = os.getenv("CONTROL_API_TOKEN", "")
CONTROL_API_LOCALHOST_ONLY = os.getenv("CONTROL_API_LOCALHOST_ONLY", "true")

# Model used when handling inbound SMS replies.
OPENCLAW_SMS_MODEL = os.getenv("OPENCLAW_SMS_MODEL", "openclaw/voice")
VOICE_SHARED_PERSONA_ENABLED = os.getenv("VOICE_SHARED_PERSONA_ENABLED", "true")
OPENCLAW_MAIN_WORKSPACE = os.getenv("OPENCLAW_MAIN_WORKSPACE", "~/.openclaw/workspace").strip()
VOICE_PERSONA_MAX_CHARS = os.getenv("VOICE_PERSONA_MAX_CHARS", "12000")

# Generate a random proxy secret on startup (Deepgram will send this back to us)
PROXY_SECRET = os.getenv("PROXY_SECRET", secrets.token_hex(16))

DEEPGRAM_TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-thalia-en")
DEEPGRAM_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

# Voice catalog — Deepgram Aura-2 voices with rich descriptions.
VOICE_CATALOG: dict[str, dict] = {
    # English voices
    "thalia":   {"model": "aura-2-thalia-en",   "gender": "female", "accent": "American",    "desc": "Warm, friendly female voice with a clear American accent. Great all-rounder, the default voice."},
    "orion":    {"model": "aura-2-orion-en",    "gender": "male",   "accent": "American",    "desc": "Deep, confident male voice with a smooth American accent. Professional and authoritative."},
    "apollo":   {"model": "aura-2-apollo-en",   "gender": "male",   "accent": "American",    "desc": "Energetic, youthful male voice with a casual American tone. Upbeat and conversational."},
    "athena":   {"model": "aura-2-athena-en",   "gender": "female", "accent": "American",    "desc": "Articulate, polished female voice. Calm and measured delivery."},
    "luna":     {"model": "aura-2-luna-en",     "gender": "female", "accent": "American",    "desc": "Soft, gentle female voice with a soothing quality. Relaxed and approachable."},
    "zeus":     {"model": "aura-2-zeus-en",     "gender": "male",   "accent": "American",    "desc": "Bold, commanding male voice with a rich low register. Strong presence."},
    "draco":    {"model": "aura-2-draco-en",    "gender": "male",   "accent": "British",     "desc": "Refined male voice with a British RP accent. Sophisticated and articulate."},
    "pandora":  {"model": "aura-2-pandora-en",  "gender": "female", "accent": "British",     "desc": "Elegant female voice with a British accent. Warm but polished."},
    "hyperion": {"model": "aura-2-hyperion-en", "gender": "male",   "accent": "Australian",  "desc": "Relaxed male voice with an Australian accent. Friendly and laid-back."},
    # Spanish
    "estrella": {"model": "aura-2-estrella-es", "gender": "female", "accent": "Mexican",     "desc": "Bright, expressive female voice in Mexican Spanish."},
    "javier":   {"model": "aura-2-javier-es",   "gender": "male",   "accent": "Mexican",     "desc": "Clear, natural male voice in Mexican Spanish."},
    "alvaro":   {"model": "aura-2-alvaro-es",   "gender": "male",   "accent": "Spain",       "desc": "Warm male voice in Castilian Spanish."},
    "celeste":  {"model": "aura-2-celeste-es",  "gender": "female", "accent": "Colombian",   "desc": "Melodic female voice in Colombian Spanish."},
    # German
    "fabian":   {"model": "aura-2-fabian-de",   "gender": "male",   "accent": "German",      "desc": "Clear, professional male voice in German."},
    "aurelia":  {"model": "aura-2-aurelia-de",  "gender": "female", "accent": "German",      "desc": "Warm, natural female voice in German."},
    "lara":     {"model": "aura-2-lara-de",     "gender": "female", "accent": "German",      "desc": "Bright, youthful female voice in German."},
    # French
    "hector":   {"model": "aura-2-hector-fr",   "gender": "male",   "accent": "French",      "desc": "Smooth, natural male voice in French."},
    "agathe":   {"model": "aura-2-agathe-fr",   "gender": "female", "accent": "French",      "desc": "Elegant, expressive female voice in French."},
    # Italian
    "cesare":   {"model": "aura-2-cesare-it",   "gender": "male",   "accent": "Italian",     "desc": "Warm, expressive male voice in Italian."},
    "livia":    {"model": "aura-2-livia-it",    "gender": "female", "accent": "Italian",     "desc": "Melodic, lively female voice in Italian."},
    # Dutch
    "lars":     {"model": "aura-2-lars-nl",     "gender": "male",   "accent": "Dutch",       "desc": "Clear, natural male voice in Dutch."},
    "daphne":   {"model": "aura-2-daphne-nl",   "gender": "female", "accent": "Dutch",       "desc": "Warm, friendly female voice in Dutch."},
    # Japanese
    "ebisu":    {"model": "aura-2-ebisu-ja",    "gender": "male",   "accent": "Japanese",    "desc": "Natural, clear male voice in Japanese."},
    "izanami":  {"model": "aura-2-izanami-ja",  "gender": "female", "accent": "Japanese",    "desc": "Soft, natural female voice in Japanese."},
}

# Voice preference file — persists across calls
VOICE_PREF_DIR = Path(os.path.expanduser("~/.deepclaw"))
VOICE_PREF_FILE = VOICE_PREF_DIR / "voice.txt"


def read_voice_preference() -> str:
    """Read the saved voice preference, falling back to DEEPGRAM_TTS_MODEL."""
    try:
        model = VOICE_PREF_FILE.read_text(encoding="utf-8").strip()
        if model:
            return model
    except OSError:
        pass
    return DEEPGRAM_TTS_MODEL


def write_voice_preference(model: str) -> None:
    """Persist a voice model preference to disk."""
    VOICE_PREF_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_PREF_FILE.write_text(model + "\n", encoding="utf-8")


def resolve_voice(query: str) -> str | None:
    """Map a voice name, model ID, or natural-language description to a model ID.

    Accepts: exact name ("orion"), exact model ("aura-2-orion-en"), or
    description keywords ("male british").  Returns model string or None.
    """
    q = query.lower().strip()

    # Exact name match
    if q in VOICE_CATALOG:
        return VOICE_CATALOG[q]["model"]

    # Exact model ID match
    for info in VOICE_CATALOG.values():
        if info["model"] == q:
            return q

    # Fuzzy score by gender + accent keywords
    best_name: str | None = None
    best_score = 0
    for name, info in VOICE_CATALOG.items():
        score = 0
        if "male" in q and "female" not in q and info["gender"] == "male":
            score += 3
        elif "female" in q and info["gender"] == "female":
            score += 3
        accent = info["accent"].lower()
        for kw in ["american", "british", "australian", "mexican", "spain",
                    "colombian", "german", "french", "italian", "dutch", "japanese"]:
            if kw in q and kw in accent:
                score += 5
        if name in q:
            score += 10
        if score > best_score:
            best_score = score
            best_name = name
    if best_name and best_score > 0:
        return VOICE_CATALOG[best_name]["model"]
    return None

app = FastAPI(title="deepclaw-voice-agent")


@app.on_event("shutdown")
async def _shutdown_http_client():
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# Map Deepgram caller IP → OpenClaw session key for the active call.
# Deepgram sends LLM requests from a fixed IP per call session, so we
# use the caller IP to correlate proxy requests with the right call.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared httpx client for OpenClaw requests (connection pooling)."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=60.0)
    return _http_client


_active_sessions: dict[str, str] = {}
_active_call_context: str = ""  # context for the current outbound call
_outbound_call_contexts: dict[str, str] = {}  # call SID → context for the voice agent


def as_bool_env(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def as_int_env(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def is_owner_phone(phone_number: str | None) -> bool:
    if not OWNER_PHONE:
        return False
    return str(phone_number or "").strip() == OWNER_PHONE


def is_local_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip().lower()
    if host in {"localhost", "testclient", "host.docker.internal"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_twilio_client():
    if TwilioClient is None:
        return None
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return None
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def build_public_url_for_request(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}".rstrip("/")


def get_public_host(request: Request) -> str:
    base_url = build_public_url_for_request(request)
    return base_url.replace("https://", "").replace("http://", "").rstrip("/")


def build_twilio_validation_url(request: Request) -> str:
    base = build_public_url_for_request(request)
    query = request.url.query
    if query:
        return f"{base}{request.url.path}?{query}"
    return f"{base}{request.url.path}"


def validate_twilio_form_request(request: Request, form_data) -> bool:
    if not as_bool_env(TWILIO_VALIDATE_SIGNATURES, default=True):
        return True
    if RequestValidator is None:
        logger.error("Twilio signature validation requested, but twilio package is missing")
        return False
    signature = request.headers.get("x-twilio-signature", "")
    if not signature:
        return False
    if not TWILIO_AUTH_TOKEN:
        logger.error("Twilio signature validation requested, but TWILIO_AUTH_TOKEN is missing")
        return False
    validation_url = build_twilio_validation_url(request)
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    try:
        params = {key: str(value) for key, value in form_data.items()}
        return bool(validator.validate(validation_url, params, signature))
    except Exception as exc:
        logger.warning("Twilio signature validation failed: %s", exc)
        return False


async def read_twilio_form_data(request: Request) -> dict[str, str]:
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def control_auth_error(request: Request):
    if as_bool_env(CONTROL_API_LOCALHOST_ONLY, default=True) and not is_local_request(request):
        return Response(
            content=json.dumps({"error": "forbidden: control endpoint is localhost-only"}),
            status_code=403,
            media_type="application/json",
        )
    if not CONTROL_API_TOKEN:
        return Response(
            content=json.dumps({"error": "control api token is not configured"}),
            status_code=503,
            media_type="application/json",
        )
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {CONTROL_API_TOKEN}"
    if auth != expected:
        return Response(
            content=json.dumps({"error": "unauthorized"}),
            status_code=401,
            media_type="application/json",
        )
    return None


def extract_text_from_completion_response(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0] or {}
    message = first_choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def load_workspace_prompt_file(workspace: str, filename: str) -> str:
    path = Path(os.path.expanduser(workspace)) / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_shared_persona_prompt() -> str:
    if not as_bool_env(VOICE_SHARED_PERSONA_ENABLED, default=True):
        return ""

    workspace = OPENCLAW_MAIN_WORKSPACE or "~/.openclaw/workspace"
    sections = [
        ("SOUL.md", "SOUL"),
        ("IDENTITY.md", "IDENTITY"),
        ("USER.md", "USER"),
    ]

    chunks: list[str] = []
    for filename, label in sections:
        content = load_workspace_prompt_file(workspace, filename)
        if content:
            chunks.append(f"[{label}] ({filename})\n{content}")

    if not chunks:
        return ""

    prompt = (
        "Use this as the canonical OpenClaw personality and relationship context.\n"
        "Prioritize these sections in order, while staying concise for voice.\n\n"
        + "\n\n".join(chunks)
    )
    max_chars = as_int_env(VOICE_PERSONA_MAX_CHARS, default=12000)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars].rstrip() + "\n\n[PERSONA TRUNCATED]"
    return prompt


def build_voice_behavior_overlay_prompt() -> str:
    parts = [
        "Voice behavior policy:\n"
        "- Speak naturally in plain sentences without markdown, bullets, or emojis.\n"
        "- Keep responses concise unless the caller asks for more detail.\n"
        "- If an operation may take longer than 2 seconds (2s), give a brief heads-up first.\n"
        "- If an operation may take more than 6 seconds or has side effects, ask for confirmation before running it.\n"
        "- Prefer short progress updates instead of long step-by-step narration.\n"
        "- When your response contains URLs, addresses, phone numbers, code snippets, "
        "lists, or other structured data that is hard to convey by voice, or a very long "
        'response with lots of details, say "I\'ll text that to you" and immediately '
        "send it via the SMS control API (POST /api/sms) so the caller has a written copy. "
        "Keep the spoken summary brief.",
    ]
    if CONTROL_API_TOKEN:
        port = os.getenv("PORT", "8000")
        parts.append(
            "\n\nLocal control API (deepclaw):\n"
            "You can place an outbound phone call to the owner by running:\n"
            f'  curl -s -X POST http://127.0.0.1:{port}/api/call '
            f'-H "Authorization: Bearer {CONTROL_API_TOKEN}" -H "Content-Type: application/json"\n'
            "You can send an outbound SMS to the owner by running:\n"
            f'  curl -s -X POST http://127.0.0.1:{port}/api/sms '
            f'-H "Authorization: Bearer {CONTROL_API_TOKEN}" -H "Content-Type: application/json" '
            '-d \'{"message": "your message here"}\'\n\n'
            "Voice selection:\n"
            "When the caller asks to change your voice, accent, or how you sound, "
            "YOU CAN DO IT. Use the voice API below. Do not say it's a system setting "
            "or that you can't — you have full control.\n"
            "First GET the catalog to pick the right voice, then POST to set it.\n"
            "To list available voices:\n"
            f'  curl -s http://127.0.0.1:{port}/api/voice '
            f'-H "Authorization: Bearer {CONTROL_API_TOKEN}"\n'
            "To change the voice:\n"
            f'  curl -s -X POST http://127.0.0.1:{port}/api/voice '
            f'-H "Authorization: Bearer {CONTROL_API_TOKEN}" -H "Content-Type: application/json" '
            '-d \'{"voice": "<name or model id>"}\'\n'
            "Tell the caller you've updated the voice and it will take effect on the next call.\n\n"
            "Scheduling reminders and future calls:\n"
            "Use the cron tool to schedule calls or texts at a future time.\n"
            "For a one-shot reminder, use schedule.kind \"at\" with an ISO-8601 UTC timestamp.\n"
            "Always set sessionTarget to \"isolated\" and deleteAfterRun to true.\n"
            "IMPORTANT: Do NOT set delivery.mode. Omit the delivery field entirely.\n"
            "The payload should be an agentTurn message telling the isolated agent to run the curl command above.\n"
            "Example cron tool call for \"call me in 5 minutes\" (adjust timestamp using Current UTC time):\n"
            '  action: "add"\n'
            '  job.name: "call-owner-reminder" (required — give each job a descriptive name)\n'
            '  job.schedule: {"kind": "at", "at": "<ISO-8601 UTC timestamp 5 min from now>"}\n'
            '  job.sessionTarget: "isolated"\n'
            '  job.deleteAfterRun: true\n'
            '  job.payload: {"kind": "agentTurn", "message": "Run this command now: '
            f"curl -s -X POST http://127.0.0.1:{port}/api/call "
            f"-H 'Authorization: Bearer {CONTROL_API_TOKEN}' -H 'Content-Type: application/json' "
            '-d \'{\\"context\\": \\"<why you are calling>\\"}\'"}\n'
            "Always include a context field in the /api/call body describing why you are calling.\n"
            "For SMS reminders, replace /api/call with /api/sms and add -d with the message."
        )
    return "\n".join(parts)


async def generate_sms_reply(inbound_text: str, session_key: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
        "X-OpenClaw-Session-Key": session_key,
    }
    # Inject the same developer messages as the voice proxy so SMS shares
    # persona, behavior overlay, and current time for cron scheduling.
    injected: list[dict[str, str]] = []
    shared_persona_prompt = build_shared_persona_prompt()
    if shared_persona_prompt:
        injected.append({"role": "developer", "content": shared_persona_prompt})
    injected.append({"role": "developer", "content": build_voice_behavior_overlay_prompt()})
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    injected.append({"role": "developer", "content": f"Current UTC time: {now_utc}"})
    body = {
        "model": OPENCLAW_SMS_MODEL,
        "stream": False,
        "messages": injected + [{"role": "user", "content": inbound_text}],
    }
    try:
        client = get_http_client()
        response = await client.post(
            f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
            json=body,
            headers=headers,
        )
        if response.status_code >= 400:
            logger.warning(
                "OpenClaw SMS reply failed: status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
            return ""
        payload = response.json()
        return extract_text_from_completion_response(payload)
    except Exception as exc:
        logger.warning("OpenClaw SMS reply request failed: %s", exc)
        return ""


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
    # Build the same message prefix that the LLM proxy injects,
    # so the Anthropic prompt cache built here matches real requests.
    messages: list[dict[str, str]] = []
    shared_persona_prompt = build_shared_persona_prompt()
    if shared_persona_prompt:
        messages.append({"role": "developer", "content": shared_persona_prompt})
    messages.append({"role": "developer", "content": build_voice_behavior_overlay_prompt()})
    messages.append({"role": "user", "content": "warmup"})
    body = {
        "model": "openclaw/voice",
        "stream": True,
        "messages": messages,
    }
    try:
        client = get_http_client()
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
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    injected_messages: list[dict[str, str]] = []
    shared_persona_prompt = build_shared_persona_prompt()
    if shared_persona_prompt:
        injected_messages.append({"role": "developer", "content": shared_persona_prompt})
    injected_messages.append({"role": "developer", "content": build_voice_behavior_overlay_prompt()})
    # Inject current UTC time so the agent can compute correct future timestamps
    # for cron scheduling, reminders, etc.  Kept as a separate message so the
    # persona + overlay prefix stays stable for Anthropic prompt-cache hits.
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    injected_messages.append({"role": "developer", "content": f"Current UTC time: {now_utc}"})
    if _active_call_context:
        injected_messages.append({
            "role": "developer",
            "content": f"You are on an outbound call you initiated. Reason: {_active_call_context}",
        })
    body["messages"] = injected_messages + messages

    # Route to the 'voice' agent (configured with claude-haiku-4-5)
    body["model"] = "openclaw/voice"

    # Strip tools before forwarding — OpenClaw doesn't use them
    body.pop("tools", None)
    body.pop("tool_choice", None)

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
        client = get_http_client()
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
        client = get_http_client()
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

def get_agent_config(public_url: str, *, outbound: bool = False) -> dict:
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
                "prompt": (
                    "Phone-call mode. Respond in short plain sentences suitable for speech. "
                    "No markdown, bullet lists, or emojis."
                ),
            },
            "greeting": "Hey! What's up?",
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": read_voice_preference(),
                },
            },
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

SMS_TWIML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{body}</Message>
</Response>"""

REJECT_TWIML = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Reject /></Response>"""


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Handle inbound Twilio voice calls with owner-only access control."""
    form = await read_twilio_form_data(request)
    if not validate_twilio_form_request(request, form):
        return Response(status_code=403)
    caller_from = str(form.get("From", "")).strip()
    if not is_owner_phone(caller_from):
        logger.warning("Rejected inbound call from non-owner: %s", caller_from)
        return Response(content=REJECT_TWIML, media_type="application/xml")
    host = get_public_host(request)
    twiml = TWIML_TEMPLATE.format(host=host)
    logger.info("Accepted inbound owner call from %s", caller_from)
    return Response(content=twiml, media_type="application/xml")


EMPTY_TWIML = """<?xml version="1.0" encoding="UTF-8"?>\n<Response/>"""


async def _send_sms_reply(inbound_text: str, session_key: str):
    """Generate a reply via OpenClaw and send it as an outbound SMS."""
    try:
        reply = await generate_sms_reply(inbound_text, session_key)
        if not reply:
            return
        client = get_twilio_client()
        if not client:
            logger.error("Cannot send SMS reply: Twilio client unavailable")
            return
        result = client.messages.create(
            to=OWNER_PHONE,
            from_=TWILIO_PHONE_NUMBER,
            body=reply,
        )
        logger.info("SMS reply sent: %s", getattr(result, "sid", "unknown"))
    except Exception:
        logger.exception("Failed to send SMS reply")


@app.post("/twilio/sms")
async def twilio_inbound_sms(request: Request):
    """Handle inbound Twilio SMS with owner-only policy."""
    form = await read_twilio_form_data(request)
    if not validate_twilio_form_request(request, form):
        return Response(status_code=403)
    sender = str(form.get("From", "")).strip()
    if not is_owner_phone(sender):
        logger.warning("Ignoring inbound SMS from non-owner: %s", sender)
        return Response(status_code=204)
    inbound_text = str(form.get("Body", "")).strip()
    if not inbound_text:
        return Response(status_code=204)
    session_key = f"agent:voice:owner:{OWNER_PHONE}"
    asyncio.create_task(_send_sms_reply(inbound_text, session_key))
    return Response(content=EMPTY_TWIML, media_type="application/xml")


@app.websocket("/twilio/media")
async def twilio_media_websocket(websocket: WebSocket):
    """Bridge Twilio media stream to Deepgram Voice Agent API."""
    global _active_call_context
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    stream_sid: str | None = None
    session_key: str | None = None
    deepgram_ws = None
    sender_task = None
    receiver_task = None
    prewarm_task = None

    # Queue for forwarding audio packets immediately (no batching)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_to_deepgram():
        """Forward audio from Twilio to Deepgram immediately."""
        while True:
            chunk = await audio_queue.get()
            if deepgram_ws:
                try:
                    await deepgram_ws.send(chunk)
                except Exception as e:
                    logger.error(f"Error sending to Deepgram: {e}")
                    break

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
                start_meta = message.get("start", {})
                call_sid = start_meta.get("callSid", "")

                # Check if this is an outbound call we initiated
                _sentinel = object()
                call_context = _outbound_call_contexts.pop(call_sid, _sentinel)
                is_outbound = call_context is not _sentinel
                call_context = call_context if is_outbound else ""

                # Get the public URL from the websocket headers
                host = websocket.headers.get("host", "localhost:8000")
                public_url = f"https://{host}"

                logger.info(f"Stream started: {stream_sid} (outbound={is_outbound})")
                logger.info(f"Public URL for LLM proxy: {public_url}")

                # Create a stable session key for this call and register
                # it so the LLM proxy can find it when Deepgram calls back.
                session_key = f"agent:voice:owner:{OWNER_PHONE}"
                _active_sessions[public_url] = session_key
                # Deepgram calls us from its cloud IPs — register a
                # catch-all so any caller hitting /v1/chat/completions
                # during this call gets the right session.
                _active_sessions["_current"] = session_key
                _active_call_context = call_context or ""
                if call_context:
                    logger.info(f"Outbound call context: {call_context[:100]}")
                logger.info(f"Session key: {session_key}")

                # Pre-warm the OpenClaw session in the background so the
                # prompt cache is hot by the time the user speaks.
                prewarm_task = asyncio.create_task(
                    prewarm_openclaw_session(session_key)
                )

                # Now send agent config with correct URL and greeting
                config = get_agent_config(public_url, outbound=is_outbound)
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
                # Decode and forward audio immediately
                payload = message.get("media", {}).get("payload", "")
                if payload:
                    audio_data = base64.b64decode(payload)
                    audio_queue.put_nowait(audio_data)

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
        _active_call_context = ""
        logger.info("Cleanup complete")


# ============================================================================
# Outbound Control API (localhost + bearer token)
# ============================================================================

@app.post("/api/call")
async def initiate_owner_call(request: Request):
    auth_error = control_auth_error(request)
    if auth_error is not None:
        return auth_error

    if not OWNER_PHONE:
        return Response(
            content=json.dumps({"error": "OWNER_PHONE is not configured"}),
            status_code=500,
            media_type="application/json",
        )

    twilio_client = get_twilio_client()
    if not twilio_client:
        return Response(
            content=json.dumps({"error": "Twilio credentials not configured"}),
            status_code=500,
            media_type="application/json",
        )

    if not TWILIO_PHONE_NUMBER:
        return Response(
            content=json.dumps({"error": "TWILIO_PHONE_NUMBER not configured"}),
            status_code=500,
            media_type="application/json",
        )

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    call_context = str(body.get("context", "")).strip()

    public_host = get_public_host(request)
    twiml = TWIML_TEMPLATE.format(host=public_host)
    try:
        call = twilio_client.calls.create(
            to=OWNER_PHONE,
            from_=TWILIO_PHONE_NUMBER,
            twiml=twiml,
            machine_detection="Enable",
            async_amd_status_callback=f"https://{public_host}/twilio/amd",
            async_amd_status_callback_method="POST",
            status_callback=f"https://{public_host}/twilio/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        call_sid = getattr(call, "sid", "")
        if call_sid:
            _outbound_call_contexts[call_sid] = call_context
        logger.info("Outbound owner call created: %s", call_sid or "unknown")
        return {
            "call_sid": getattr(call, "sid", ""),
            "status": getattr(call, "status", "queued"),
            "to": OWNER_PHONE,
        }
    except TwilioRestException as exc:
        return Response(
            content=json.dumps({"error": str(exc), "code": getattr(exc, "code", None)}),
            status_code=500,
            media_type="application/json",
        )


@app.post("/api/sms")
async def send_owner_sms(request: Request):
    auth_error = control_auth_error(request)
    if auth_error is not None:
        return auth_error

    if not OWNER_PHONE:
        return Response(
            content=json.dumps({"error": "OWNER_PHONE is not configured"}),
            status_code=500,
            media_type="application/json",
        )

    twilio_client = get_twilio_client()
    if not twilio_client:
        return Response(
            content=json.dumps({"error": "Twilio credentials not configured"}),
            status_code=500,
            media_type="application/json",
        )

    if not TWILIO_PHONE_NUMBER:
        return Response(
            content=json.dumps({"error": "TWILIO_PHONE_NUMBER not configured"}),
            status_code=500,
            media_type="application/json",
        )

    body = await request.json()
    message = str(body.get("message", "")).strip()
    if not message:
        return Response(
            content=json.dumps({"error": "Missing 'message' field"}),
            status_code=400,
            media_type="application/json",
        )

    try:
        result = twilio_client.messages.create(
            to=OWNER_PHONE,
            from_=TWILIO_PHONE_NUMBER,
            body=message,
        )
        logger.info("Outbound owner SMS sent: %s", getattr(result, "sid", "unknown"))
        return {
            "message_sid": getattr(result, "sid", ""),
            "status": getattr(result, "status", "queued"),
            "to": OWNER_PHONE,
        }
    except TwilioRestException as exc:
        return Response(
            content=json.dumps({"error": str(exc), "code": getattr(exc, "code", None)}),
            status_code=500,
            media_type="application/json",
        )


@app.get("/api/voice")
async def get_voice_preference(request: Request):
    """Return the current voice and the full catalog."""
    auth_error = control_auth_error(request)
    if auth_error is not None:
        return auth_error

    current_model = read_voice_preference()
    current_name = None
    for name, info in VOICE_CATALOG.items():
        if info["model"] == current_model:
            current_name = name
            break

    catalog = [
        {
            "name": name,
            "model": info["model"],
            "gender": info["gender"],
            "accent": info["accent"],
            "description": info["desc"],
        }
        for name, info in VOICE_CATALOG.items()
    ]
    return {
        "current": {"name": current_name, "model": current_model},
        "voices": catalog,
    }


@app.post("/api/voice")
async def set_voice_preference(request: Request):
    """Set the voice for future calls. Accepts name or model ID."""
    auth_error = control_auth_error(request)
    if auth_error is not None:
        return auth_error

    body = await request.json()
    voice_input = str(body.get("voice", "")).strip()
    if not voice_input:
        return Response(
            content=json.dumps({"error": "Missing 'voice' field. Use a name (e.g. 'orion') or model ID."}),
            status_code=400,
            media_type="application/json",
        )

    model = resolve_voice(voice_input)
    if not model:
        return Response(
            content=json.dumps({
                "error": f"Unknown voice: {voice_input}",
                "hint": "Use GET /api/voice to see available voices.",
            }),
            status_code=400,
            media_type="application/json",
        )

    write_voice_preference(model)
    voice_name = None
    for name, info in VOICE_CATALOG.items():
        if info["model"] == model:
            voice_name = name
            break
    logger.info("Voice preference set to %s (%s)", voice_name, model)
    return {
        "voice": {"name": voice_name, "model": model},
        "message": "Voice updated. The change takes effect on the next call.",
    }


@app.post("/twilio/status")
async def twilio_status_callback(request: Request):
    form = await read_twilio_form_data(request)
    if not validate_twilio_form_request(request, form):
        return Response(status_code=403)
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    logger.info("Twilio call status sid=%s status=%s", call_sid, call_status)
    return Response(status_code=200)


@app.post("/twilio/amd")
async def twilio_amd_callback(request: Request):
    """Hang up outbound calls that hit voicemail."""
    form = await read_twilio_form_data(request)
    if not validate_twilio_form_request(request, form):
        return Response(status_code=403)
    call_sid = form.get("CallSid", "")
    answered_by = form.get("AnsweredBy", "")
    logger.info("AMD result sid=%s answered_by=%s", call_sid, answered_by)
    if answered_by in ("machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"):
        twilio_client = get_twilio_client()
        if twilio_client and call_sid:
            try:
                twilio_client.calls(call_sid).update(status="completed")
                logger.info("Hung up machine-answered call: %s", call_sid)
            except Exception as exc:
                logger.warning("Failed to hang up machine call %s: %s", call_sid, exc)
    return Response(status_code=200)


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
    
    # Queue for forwarding audio packets immediately (no batching)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def send_to_deepgram():
        """Forward audio from Telnyx to Deepgram immediately."""
        while True:
            chunk = await audio_queue.get()
            if deepgram_ws:
                try:
                    await deepgram_ws.send(chunk)
                except Exception as e:
                    logger.error(f"Error sending to Deepgram: {e}")
                    break
    
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
                    audio_queue.put_nowait(audio_data)
            
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
