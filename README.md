# deepclaw

Call your OpenClaw over the phone using the [Deepgram Voice Agent API](https://developers.deepgram.com/docs/voice-agent).

## Why Deepgram?

| | ElevenLabs | Deepgram |
|---|---|---|
| **Turn detection** | VAD-based | Semantic (Flux) |
| **TTS latency** | ~200ms TTFB | 90ms TTFB |
| **TTS price** | $0.050/1K chars | $0.030/1K chars |
| **Barge-in** | Basic VAD | Native StartOfTurn |

Deepgram Flux understands *when you're done talking* semantically and acoustically—not just when you stop making noise. This means fewer awkward interruptions and faster responses.

## Provider Comparison: Twilio vs Telnyx

| Feature | Twilio | Telnyx |
|---------|--------|--------|
| **Setup Complexity** | Moderate | Easy |
| **Phone Number Cost** | ~$1/month | ~$0.50-$2/month |
| **Call Pricing** | $0.085/min | $0.005-$0.025/min |
| **Media Streaming** | WebSocket + TwiML | WebSocket + REST API |
| **Authentication** | Account SID + Auth Token | API Key + Public Key |
| **Documentation** | Extensive | Growing |
| **Global Coverage** | Excellent | Excellent |

**Recommendation:**
- **Twilio**: Better for production apps with extensive docs and ecosystem
- **Telnyx**: More cost-effective, simpler API, better for experimenting

## How It Works

deepclaw uses the [Deepgram Voice Agent API](https://developers.deepgram.com/docs/voice-agent)—a single WebSocket that handles STT, TTS, turn-taking, and barge-in together.

```
Phone Call → Twilio/Telnyx → deepclaw ←──WebSocket──→ Deepgram Voice Agent API
                                │                      (Flux STT + Aura-2 TTS)
                                │
                                ↓
                           OpenClaw (LLM)
```

1. You call your phone number
2. **Twilio/Telnyx** streams audio to deepclaw via WebSocket
3. deepclaw forwards audio to Deepgram Voice Agent API
4. Flux transcribes with semantic turn detection
5. Deepgram calls your LLM endpoint (OpenClaw via deepclaw proxy)
6. Aura-2 speaks the response, streamed back through your phone provider

**Barge-in support:** Start talking while the assistant is speaking and it stops immediately—handled natively by the Voice Agent API.

## Quick Setup (Let OpenClaw Do It)

The easiest way to set up deepclaw is to let your OpenClaw do it for you:

```bash
# Copy the skill to your OpenClaw
cp -r skills/deepclaw-voice ~/.openclaw/skills/
```

Then tell your OpenClaw: **"I want to call you on the phone"**

OpenClaw will walk you through:
- Creating a Deepgram account (free $200 credit)
- Setting up a Twilio phone number (~$1/month)
- Configuring everything automatically

## Manual Setup

### Prerequisites

- Python 3.10+
- [Deepgram account](https://console.deepgram.com/) (free tier available, $200 credit)
- **Phone Provider** (choose one):
  - [Twilio account](https://www.twilio.com/) with a phone number (~$1/month)
  - [Telnyx account](https://telnyx.com/) with a phone number (~$0.50-$2/month)
- [OpenClaw](https://github.com/openclaw/openclaw) running locally
- [ngrok](https://ngrok.com/) for exposing your local server

### 1. Clone and install

```bash
git clone https://github.com/deepgram/deepclaw.git
cd deepclaw
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

**For Twilio (default):**
```env
DEEPGRAM_API_KEY=your_deepgram_api_key
VOICE_PROVIDER=twilio
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+15551234567
OWNER_PHONE=+15551234567
TWILIO_VALIDATE_SIGNATURES=true
PUBLIC_URL=https://your-ngrok-url.ngrok-free.app
CONTROL_API_TOKEN=replace_with_random_secret
CONTROL_API_LOCALHOST_ONLY=true
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=your_openclaw_gateway_token
VOICE_SHARED_PERSONA_ENABLED=true
OPENCLAW_MAIN_WORKSPACE=~/.openclaw/workspace
VOICE_PERSONA_MAX_CHARS=12000
```

**For Telnyx:**
```env
DEEPGRAM_API_KEY=your_deepgram_api_key
VOICE_PROVIDER=telnyx
TELNYX_API_KEY=your_telnyx_api_key
TELNYX_PUBLIC_KEY=your_telnyx_public_key
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=your_openclaw_gateway_token
VOICE_SHARED_PERSONA_ENABLED=true
OPENCLAW_MAIN_WORKSPACE=~/.openclaw/workspace
VOICE_PERSONA_MAX_CHARS=12000
```

### 3. Configure OpenClaw

Enable the chat completions endpoint in your `openclaw.json`:

```bash
openclaw config set gateway.http.endpoints.chatCompletions.enabled true
```

deepclaw automatically creates a dedicated `voice` agent in OpenClaw that uses a fast model (Claude Haiku 4.5) for low-latency phone conversations. This happens on first `python -m deepclaw` launch. To customize the model:

```bash
# Use a different model for voice (optional)
export OPENCLAW_VOICE_MODEL=anthropic/claude-sonnet-4-5-20250929
```

Or create the agent manually:

```bash
openclaw agents add voice --model anthropic/claude-haiku-4-5-20251001
```

### Voice Personality and Tool Behavior

deepclaw keeps the fast `openclaw/voice` model route, but now injects two prompts into voice proxy requests:

- **Shared personality context** from your main OpenClaw workspace files:
  - `SOUL.md`
  - `IDENTITY.md`
  - `USER.md`
- **Voice behavior overlay** with phone-specific policy:
  - concise spoken responses
  - heads-up before operations expected to take more than 2 seconds
  - confirmation before operations expected to take more than 6 seconds or cause side effects

This means you can keep one core personality for chat and voice, while still using voice-safe response behavior.

Notes:
- This does **not** include automatic long-task SMS follow-up yet.
- If voice feels off-persona, verify `OPENCLAW_MAIN_WORKSPACE` and `VOICE_SHARED_PERSONA_ENABLED`.

### 4. Start the tunnel

```bash
ngrok http 8000
```

Note your ngrok URL (e.g., `https://abc123.ngrok-free.app`).

### 5. Configure Your Phone Provider

#### Option A: Configure Twilio

1. Go to your [Twilio Console](https://console.twilio.com/)
2. Navigate to Phone Numbers → Manage → Active Numbers
3. Click your number
4. Under "Voice Configuration":
   - Set "A Call Comes In" to **Webhook**
   - URL: `https://your-ngrok-url.ngrok-free.app/twilio/incoming`
   - Method: **POST**
5. Under "Messaging Configuration":
   - Set "A Message Comes In" to **Webhook**
   - URL: `https://your-ngrok-url.ngrok-free.app/twilio/sms`
   - Method: **POST**
6. Save

#### Option B: Configure Telnyx

1. Go to your [Telnyx Mission Control Portal](https://portal.telnyx.com/)
2. Navigate to **Voice → Programmable Voice**
3. Create a new **Voice API Application**:
   - **Application Name**: `deepclaw-voice`
   - **Webhook URL**: `https://your-ngrok-url.ngrok-free.app/telnyx/webhook`
   - **Webhook API Version**: `API v2` (recommended)
   - **Webhook Failover URL**: (optional) same as webhook URL
4. Click **Create**
5. Go to **Numbers → My Numbers**
6. Click your phone number
7. Under **Voice Settings**:
   - **Connection**: Select your `deepclaw-voice` application
8. Save configuration

#### Getting Telnyx API Keys

1. In Mission Control Portal, go to **API Keys**
2. Create a new API Key or copy existing one
3. For **Public Key**: Go to **Account → Public Key** and copy the key

### 6. Start deepclaw

```bash
python -m deepclaw
```

### 7. Call your number

Pick up the phone and talk to your OpenClaw!

### 8. Trigger outbound call/text securely (optional)

deepclaw exposes local control endpoints that are intended for trusted local callers only:

- `POST /api/call` - trigger an outbound call to `OWNER_PHONE`
- `POST /api/sms` - send an outbound SMS to `OWNER_PHONE`

Both require:

- `Authorization: Bearer $CONTROL_API_TOKEN`
- local-origin access when `CONTROL_API_LOCALHOST_ONLY=true` (default)

Examples:

```bash
curl -X POST http://127.0.0.1:8000/api/call \
  -H "Authorization: Bearer $CONTROL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'

curl -X POST http://127.0.0.1:8000/api/sms \
  -H "Authorization: Bearer $CONTROL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Heartbeat check: all systems normal."}'
```

## Architecture

```
┌─────────────┐     ┌──────────────────────────────────────────────────────┐
│   Caller    │     │                   Your Machine                        │
│  (Phone)    │     │                                                       │
└──────┬──────┘     │  ┌───────────┐   ┌───────────┐   ┌───────────────┐   │
       │            │  │ Twilio or │   │ deepclaw  │   │   OpenClaw    │   │
       │ PSTN       │  │  Telnyx   │──▶│  Server   │──▶│   Gateway     │   │
       │            │  │ Webhook   │   └─────┬─────┘   └───────────────┘   │
       ▼            │  └───────────┘         │                              │
┌──────────────┐    │                        │ WebSocket                    │
│   Twilio/    │◀───┼────────────────────────┤                              │
│   Telnyx     │    │                        ▼                              │
│ (SIP/Media)  │    │              ┌───────────────────┐                    │
└──────────────┘    │              │ Deepgram Voice    │                    │
       │            │              │ Agent API         │                    │
       │  Audio     │              │ • Flux (STT)      │                    │
       └────────────┼─────────────▶│ • Aura-2 (TTS)    │                    │
                    │              │ • Turn detection  │                    │
                    │              │ • Barge-in        │                    │
                    │              └───────────────────┘                    │
                    └──────────────────────────────────────────────────────┘
```

The Voice Agent API handles the entire speech pipeline in a single WebSocket connection. deepclaw bridges Twilio's media stream to the Voice Agent API and proxies LLM requests to your local OpenClaw.

## Customizing Voice

deepclaw uses Deepgram Aura-2 TTS with 80+ voices in 7 languages. Edit `voice_agent_server.py`:

```python
"speak": {
    "provider": {
        "type": "deepgram",
        "model": "aura-2-orion-en",  # Change voice here
    },
},
```

**Popular voices:**
| Voice | Style |
|-------|-------|
| `aura-2-thalia-en` | Feminine, American (default) |
| `aura-2-orion-en` | Masculine, American |
| `aura-2-draco-en` | Masculine, British |
| `aura-2-estrella-es` | Feminine, Mexican Spanish |
| `aura-2-fabian-de` | Masculine, German |

See `skills/deepclaw-voice/SKILL.md` for the complete voice list (80+ voices in 7 languages), or test voices at https://playground.deepgram.com/

## Security Considerations

Be aware of these security considerations when using OpenClaw and deepclaw. Like the rest of OpenClaw, use at your own risk.

**1. LLM proxy endpoint has no authentication**
- The `/v1/chat/completions` endpoint is unauthenticated
- Anyone who discovers your ngrok URL can use your OpenClaw/Anthropic API credits
- **Mitigation:** Keep your ngrok URL private. Consider using a fixed ngrok domain.

**2. Twilio webhook signature validation must stay enabled**
- deepclaw validates `X-Twilio-Signature` by default (`TWILIO_VALIDATE_SIGNATURES=true`)
- Signature checks depend on the public URL; set `PUBLIC_URL` to your exact external webhook base URL
- **Mitigation:** Do not disable signature validation outside local debugging.

**3. Owner-only policy is enforced server-side**
- Inbound voice and SMS are only accepted from `OWNER_PHONE`
- Outbound `/api/call` and `/api/sms` always target `OWNER_PHONE` (caller-provided destination is ignored)
- **Mitigation:** Set `OWNER_PHONE` explicitly and treat it as the single source of truth.

**4. Control endpoints are sensitive**
- `/api/call` and `/api/sms` require bearer auth via `CONTROL_API_TOKEN`
- `CONTROL_API_LOCALHOST_ONLY=true` restricts these endpoints to local callers by default
- **Mitigation:** Keep the token secret and avoid exposing control endpoints publicly.

**5. Credentials in `.env` file**
- API keys and tokens are stored in plaintext
- **Mitigation:** The file is gitignored. Set restrictive permissions: `chmod 600 .env`

**6. ngrok exposes your local machine**
- Your server is accessible from the internet while running
- **Mitigation:** Only run when needed. Use ngrok's IP allowlist on paid plans.

**For production deployments**, consider:
- Keeping Twilio signature validation enabled at all times
- Running with owner-only policy (`OWNER_PHONE`) instead of open caller lists
- Keeping control endpoints local-only and token-protected
- Running behind a reverse proxy with rate limiting
- Using a dedicated server instead of ngrok
- Implementing proper authentication on the LLM proxy

## Known Limitations

**OpenClaw streaming latency:** OpenClaw's `/v1/chat/completions` endpoint currently buffers responses for ~5 seconds before streaming begins. This adds latency to LLM responses regardless of which voice provider you use (Deepgram, ElevenLabs, etc.).

The initial greeting is instant (generated by Deepgram), but subsequent responses wait for OpenClaw's buffer.

This is an upstream limitation. OpenClaw's native WebSocket agent endpoint streams properly, but external voice APIs require the OpenAI-compatible chat completions endpoint.

**No automatic follow-up orchestration yet:** deepclaw does not yet send guaranteed background follow-up SMS/call updates for long-running tasks.

## Coming Soon

- **Local wake-word mode** — Talk to OpenClaw hands-free at your desk, no phone needed
- **One-click desktop installer** — No terminal required
- **Native OpenClaw plugin** — Install with one command

## Getting Help

If you run into issues:

1. **Check existing issues:** Search [GitHub Issues](https://github.com/deepgram/deepclaw/issues) to see if your problem has been reported
2. **Open a new issue:** Include:
   - What you were trying to do
   - What happened instead
   - Server logs (redact any API keys)
   - Your environment (OS, Python version, OpenClaw version)
3. **Deepgram support:** For Deepgram-specific issues, visit [Deepgram's Community](https://discord.gg/deepgram)

## Contributing

Contributions are welcome! Here's how to help:

### Reporting Bugs

Open an issue with:
- Clear description of the bug
- Steps to reproduce
- Expected vs actual behavior
- Logs and environment info

### Suggesting Features

Open an issue describing:
- The problem you're trying to solve
- Your proposed solution
- Any alternatives you've considered

### Pull Requests

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Test thoroughly
5. Commit with clear messages
6. Push to your fork
7. Open a PR against `main`

**Note:** The `main` branch is protected. All changes require a pull request and review.

### Code Style

- Follow existing code patterns
- Add comments for complex logic
- Update documentation for user-facing changes

## License

MIT

## Credits

Built with:
- [Deepgram Voice Agent API](https://developers.deepgram.com/docs/voice-agent-api) — Real-time conversational AI pipeline
- [Deepgram Flux](https://deepgram.com/product/speech-to-text) — Semantic speech recognition
- [Deepgram Aura-2](https://deepgram.com/product/text-to-speech) — Low-latency text-to-speech
- [OpenClaw](https://github.com/openclaw/openclaw) — Open-source AI assistant
- [Twilio](https://www.twilio.com/) — Phone infrastructure
