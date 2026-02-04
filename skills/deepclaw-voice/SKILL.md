---
name: deepclaw-voice
description: Set up phone calling to OpenClaw using Deepgram Voice Agent API
requires:
  bins:
    - python3
    - ngrok
    - git
---

# deepclaw Voice Setup

Use this skill when the user wants to call you on the phone, set up voice calling, or talk to OpenClaw via phone.

## What This Sets Up

Phone calls to OpenClaw using:
- **Deepgram Voice Agent API** - STT, TTS, turn-taking, barge-in
- **Twilio** - Phone number routing
- **OpenClaw** - Your AI (via chat completions proxy)

## Setup Process

### Step 1: Clone the repo

```bash
git clone https://github.com/deepgram/deepclaw.git ~/deepclaw
cd ~/deepclaw
pip install -r requirements.txt
```

### Step 2: Get Deepgram API Key

1. Go to https://console.deepgram.com/
2. Sign up (free $200 credit)
3. **API Keys** → **Create API Key** → Name: "deepclaw", Full Access
4. Copy key immediately

Ask: "What's your Deepgram API key?"

### Step 3: Get Twilio Credentials

1. Go to https://www.twilio.com/ and sign up
2. Copy **Account SID** and **Auth Token** from dashboard
3. **Phone Numbers** → **Buy a number** with Voice (~$1/month)

Ask: "What's your Twilio phone number, Account SID, and Auth Token?"

### Step 4: Get OpenClaw Gateway Token

Run this to get the token from their OpenClaw config:
```bash
grep -A2 '"auth"' ~/.openclaw/openclaw.json | grep token
```

Or generate a new one:
```bash
openssl rand -hex 24
```

If generating new, tell them to add it to `~/.openclaw/openclaw.json` under `gateway.auth.token`.

### Step 5: Create .env file

Create `~/deepclaw/.env` with their values:
```
DEEPGRAM_API_KEY=<their_deepgram_key>
TWILIO_ACCOUNT_SID=<their_sid>
TWILIO_AUTH_TOKEN=<their_token>
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
OPENCLAW_GATEWAY_TOKEN=<their_gateway_token>
```

### Step 6: Ensure OpenClaw Gateway has chat completions enabled

Check their `~/.openclaw/openclaw.json` has:
```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    }
  }
}
```

If not, add it and restart the gateway: `openclaw daemon restart`

### Step 7: Start ngrok

```bash
ngrok http 8000
```

Note the HTTPS URL (e.g., `https://abc123.ngrok-free.app`).

### Step 8: Configure Twilio Webhook

1. https://console.twilio.com/
2. **Phone Numbers** → **Active Numbers** → Click their number
3. **Voice Configuration**:
   - A Call Comes In: **Webhook**
   - URL: `https://<ngrok-url>/twilio/incoming`
   - Method: **POST**
4. Save

### Step 9: Start Server

```bash
cd ~/deepclaw
python -m deepclaw.voice_agent_server
```

### Step 10: Test

Tell them: "Call your Twilio number now!"

Watch the server logs for:
- "Connected to Deepgram Voice Agent API"
- "Agent settings applied"
- "LLM proxy request received"

---

## Customizing Voice

Edit `~/deepclaw/deepclaw/voice_agent_server.py`, find `get_agent_config()`, change the `model` in `speak`:

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

**Immediate hang-up:** Check server logs for errors. Try setting `PROXY_AUTH_ENABLED=false` in .env

**ngrok URL changed:** Update Twilio webhook. Use fixed domain: `ngrok http 8000 --domain=yourname.ngrok-free.app`
