# Deepgram Voice Agent Connect

**Date:** 2026-02-04
**Status:** Proposal
**Author:** deepclaw team
**Target Audience:** Deepgram Product/Engineering

## Executive Summary

A new mode for Deepgram Voice Agent API that eliminates the need for ngrok, public servers, or webhook configuration. Users connect outbound to Deepgram, and Deepgram bridges their local LLM/agent to phone calls.

**The pitch:** "Call your AI agent from anywhere. No servers. No ngrok. No DevOps."

## Problem Statement

### Current State

Every voice agent tutorial (including Deepgram's own) requires:

1. A publicly accessible server (ngrok for dev, cloud VM for prod)
2. Twilio webhook configuration
3. TwiML Bin setup
4. Understanding of WebSocket bridging

This creates friction that kills adoption. The typical "time to first call" is 30-60 minutes for experienced developers, and impossible for non-developers.

### ElevenLabs Comparison

ElevenLabs Agents reduces some friction but still requires:
- ngrok for custom LLM endpoints
- Manual Twilio configuration

### The Goal

**Time to first call: 5 minutes.** No ngrok. No Twilio config. No servers.

## Proposed Solution

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         User's Machine                                  │
│                                                                         │
│  ┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐    │
│  │   OpenClaw  │◄────│  deepgram-agent  │────►│  Outbound WSS to  │    │
│  │   (or any   │     │     connect      │     │  Deepgram Edge    │    │
│  │    LLM)     │     │                  │     │                   │    │
│  └─────────────┘     └──────────────────┘     └─────────┬─────────┘    │
│                                                         │              │
└─────────────────────────────────────────────────────────┼──────────────┘
                                                          │
                              Internet                    │ WSS (outbound only)
                                                          │
┌─────────────────────────────────────────────────────────┼──────────────┐
│                      Deepgram Edge                      │              │
│                                                         ▼              │
│  ┌───────────────┐     ┌──────────────────┐     ┌─────────────────┐   │
│  │  Phone Number │     │   Voice Agent    │     │  Connection     │   │
│  │  (Provisioned │◄────│   Orchestrator   │◄────│  Manager        │   │
│  │   by Deepgram)│     │  (Flux + Aura-2) │     │                 │   │
│  └───────┬───────┘     └──────────────────┘     └─────────────────┘   │
│          │                                                             │
└──────────┼─────────────────────────────────────────────────────────────┘
           │
           │ PSTN
           ▼
      ┌──────────┐
      │  Caller  │
      │ (Phone)  │
      └──────────┘
```

### Key Innovation: Outbound-Only Connection

The user's machine initiates a persistent WebSocket connection to Deepgram. All communication flows through this single outbound connection:

- **Audio IN:** Deepgram sends caller audio to user
- **Audio OUT:** User sends TTS audio back to Deepgram
- **LLM Requests:** Deepgram sends transcripts, user returns responses
- **Control:** Barge-in signals, session management, etc.

**No inbound ports. No firewall rules. No ngrok.**

## User Experience

### Setup (One-Time)

```bash
# Install CLI
npm install -g @deepgram/agent-cli

# Authenticate
deepgram auth login

# Get a phone number (optional - can use existing Twilio)
deepgram agent phone provision
# → Your agent phone number: +1 (555) 123-4567
```

### Daily Usage

```bash
# Start the agent
deepgram agent connect --llm openclaw

# Or with a custom handler
deepgram agent connect --handler ./my-agent.py
```

Output:
```
✓ Connected to Deepgram Edge (us-west-2)
✓ Phone number: +1 (555) 123-4567
✓ Agent ready. Waiting for calls...

[12:34:56] Incoming call from +1 (555) 987-6543
[12:34:58] User: "Hey, what's on my calendar today?"
[12:35:01] Agent: "You have three meetings..."
[12:35:15] Call ended (duration: 19s)
```

### Integration Modes

**Mode 1: Built-in LLM (simplest)**
```bash
deepgram agent connect --llm openai --model gpt-4o
```
Deepgram handles everything. User just provides API key.

**Mode 2: Local LLM endpoint**
```bash
deepgram agent connect --llm http://localhost:18789/v1/chat/completions
```
For OpenClaw, Ollama, vLLM, etc. No ngrok needed - requests come through the outbound WebSocket.

**Mode 3: Custom handler (most flexible)**
```bash
deepgram agent connect --handler ./agent.py
```

```python
# agent.py
from deepgram_agent import handler

@handler.on_transcript
async def handle_transcript(transcript: str, context: dict):
    # Do anything - call OpenClaw, run local code, etc.
    response = await my_openclaw.chat(transcript)
    return response

@handler.on_function_call
async def handle_function(name: str, args: dict):
    # Handle function calls from the conversation
    if name == "check_calendar":
        return get_calendar_events(args["date"])
```

## Protocol Specification

### WebSocket Connection

```
wss://agent.deepgram.com/v1/connect
Authorization: Bearer <DEEPGRAM_API_KEY>
```

### Message Types (Deepgram → User)

```typescript
// Session started
{
  "type": "session.started",
  "session_id": "sess_abc123",
  "phone_number": "+15551234567"
}

// Incoming call
{
  "type": "call.started",
  "call_id": "call_xyz789",
  "caller": "+15559876543",
  "session_id": "sess_abc123"
}

// Audio from caller (after STT)
{
  "type": "transcript",
  "call_id": "call_xyz789",
  "text": "What's on my calendar today?",
  "is_final": true,
  "end_of_turn": true
}

// Raw audio (if user wants to handle STT themselves)
{
  "type": "audio.input",
  "call_id": "call_xyz789",
  "audio": "<base64 mulaw>",
  "sample_rate": 8000
}

// Barge-in detected
{
  "type": "barge_in",
  "call_id": "call_xyz789"
}

// Call ended
{
  "type": "call.ended",
  "call_id": "call_xyz789",
  "duration_ms": 19000,
  "reason": "caller_hangup"
}
```

### Message Types (User → Deepgram)

```typescript
// LLM response (Deepgram handles TTS)
{
  "type": "response.text",
  "call_id": "call_xyz789",
  "text": "You have three meetings today..."
}

// Raw audio response (user handles TTS)
{
  "type": "response.audio",
  "call_id": "call_xyz789",
  "audio": "<base64 mulaw>",
  "sample_rate": 8000
}

// Function call result
{
  "type": "function.result",
  "call_id": "call_xyz789",
  "function_id": "func_123",
  "result": {"events": [...]}
}

// Hang up
{
  "type": "call.hangup",
  "call_id": "call_xyz789"
}
```

## Configuration Options

```yaml
# ~/.deepgram/agent.yaml

# Phone configuration
phone:
  number: "+15551234567"          # Deepgram-provisioned, or...
  twilio:                         # Bring your own Twilio
    account_sid: "AC..."
    auth_token: "..."
    number: "+15559999999"

# Voice settings
voice:
  stt:
    model: "flux"                 # or "nova-3"
    language: "en-US"
  tts:
    model: "aura-2-thalia-en"
    speed: 1.0

# Agent behavior
agent:
  greeting: "Hey, this is your assistant. How can I help?"
  silence_timeout_ms: 30000       # Hang up after 30s silence
  max_duration_ms: 600000         # Max 10 min calls

# LLM configuration
llm:
  provider: "local"               # or "openai", "anthropic"
  endpoint: "http://localhost:18789/v1/chat/completions"
  model: "claude-opus-4-5"
  system_prompt: |
    You are a helpful assistant accessible via phone.
    Keep responses concise and conversational.
```

## Phone Number Options

### Option A: Deepgram-Provisioned Numbers

Deepgram partners with Twilio/bandwidth providers to offer phone numbers directly:

```bash
deepgram agent phone provision --country US
# → +1 (555) 123-4567 provisioned ($2/month)

deepgram agent phone list
deepgram agent phone release +15551234567
```

**Pricing:** $2/month per number + standard Voice Agent API rates

### Option B: Bring Your Own Twilio

User links their existing Twilio account:

```bash
deepgram agent twilio link
# Opens browser for OAuth, or prompts for API keys
```

Deepgram automatically configures the Twilio webhook to point at Deepgram's edge. User's local server is never exposed.

### Option C: SIP Trunk (Enterprise)

For enterprises with existing telephony:

```bash
deepgram agent sip configure --trunk-uri sip:trunk.company.com
```

## Pricing Model

| Component | Price |
|-----------|-------|
| Voice Agent API (STT + TTS + orchestration) | $4.50/hour |
| Deepgram-provisioned phone number | $2/month |
| Bring Your Own Twilio | $0 (user pays Twilio directly) |
| Connection time (idle, waiting for calls) | Free |

**Comparison:**
- ElevenLabs Conversational AI: $5.94/hour
- OpenAI Realtime API: ~$18/hour
- **Deepgram Voice Agent Connect: $4.50/hour**

## Implementation Phases

### Phase 1: Core Connect (4 weeks)

- [ ] WebSocket connection manager at Deepgram edge
- [ ] Protocol implementation (transcript/response flow)
- [ ] CLI tool: `deepgram agent connect`
- [ ] Local LLM endpoint support
- [ ] Basic barge-in handling

### Phase 2: Phone Integration (3 weeks)

- [ ] Twilio webhook hosted by Deepgram
- [ ] "Link Your Twilio" OAuth flow
- [ ] Deepgram-provisioned phone numbers
- [ ] Inbound call routing to connected agents

### Phase 3: Developer Experience (2 weeks)

- [ ] Python SDK with decorators (`@handler.on_transcript`)
- [ ] TypeScript SDK
- [ ] Dashboard: call logs, analytics, configuration
- [ ] `--handler` mode for custom logic

### Phase 4: Production Hardening (2 weeks)

- [ ] Multi-region edge deployment
- [ ] Connection failover and reconnection
- [ ] Rate limiting and abuse prevention
- [ ] SOC2 compliance documentation

## Competitive Positioning

### vs. ElevenLabs Agents

| Feature | ElevenLabs | Deepgram Connect |
|---------|------------|------------------|
| No ngrok for custom LLM | ❌ | ✅ |
| No Twilio config | ❌ | ✅ |
| Semantic turn detection | ❌ (VAD) | ✅ (Flux) |
| Self-hostable | ❌ | ✅ (on-prem option) |
| Price | $5.94/hr | $4.50/hr |

### vs. Bland.ai, Vapi, Retell

These platforms are fully hosted but don't support local LLMs. Deepgram Connect is the only solution that:

1. Works with local/private LLMs (OpenClaw, Ollama, enterprise models)
2. Requires zero infrastructure from the user
3. Keeps LLM data on the user's machine (privacy)

## Success Metrics

- **Time to first call:** < 5 minutes (from `npm install` to receiving a call)
- **Setup success rate:** > 90% (no "it doesn't work on my machine")
- **Developer NPS:** > 50
- **Adoption:** 1,000 connected agents within 3 months of launch

## Open Questions

1. **Idle connection limits:** How long can a connection stay open without calls? (Propose: unlimited, but ping/pong keepalive required)

2. **Multiple concurrent calls:** Should one connection handle multiple simultaneous calls? (Propose: yes, with call_id routing)

3. **Offline/reconnection:** What happens if user's machine goes offline mid-call? (Propose: play "agent disconnected" message, offer callback)

4. **Abuse prevention:** How to prevent phone number spam? (Propose: rate limits, phone verification, paid tier for high volume)

## Appendix: Why This Matters for OpenClaw

OpenClaw has 162K GitHub stars but a known pain point: difficult setup. The #1 requested feature is "easier voice integration."

Deepgram Voice Agent Connect would make OpenClaw voice-accessible with:

```bash
openclaw gateway start
deepgram agent connect --llm openclaw
```

Two commands. No ngrok. No Twilio. No webhook configuration.

This positions Deepgram as the default voice layer for the fastest-growing open-source AI agent platform.
