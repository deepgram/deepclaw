# deepclaw v1 Design

**Date:** 2026-02-04
**Status:** Approved
**Goal:** Rapid response to ElevenLabs' "Call Your OpenClaw" tweet. Ship today.

## Overview

A minimal Python package that lets you call your OpenClaw over the phone using Deepgram's voice stack (Flux STT + Aura-2 TTS). Faster, cheaper, and self-hostable alternative to ElevenLabs Agents.

## Why Deepgram?

| | ElevenLabs | Deepgram |
|---|---|---|
| Turn detection | VAD-based | Semantic (Flux) |
| TTS latency | ~200ms | 90ms |
| TTS price | $0.050/1K chars | $0.030/1K chars |
| Self-host | No | Yes |
| Barge-in | Basic | Native StartOfTurn |

## Architecture

```
┌─────────────┐     ┌─────────────────────────────────────────────────┐
│   Caller    │     │                  Your Machine                   │
│  (Phone)    │     │                                                 │
└──────┬──────┘     │  ┌───────────┐   ┌──────────┐   ┌───────────┐  │
       │            │  │  Twilio   │   │ deepclaw │   │ OpenClaw  │  │
       │ PSTN       │  │  Webhook  │──▶│  Server  │──▶│  Gateway  │  │
       │            │  └───────────┘   └────┬─────┘   └───────────┘  │
       ▼            │                       │                        │
┌──────────────┐    │         ┌─────────────┴─────────────┐          │
│    Twilio    │◀───┼─────────│                           │          │
│  (SIP/Media) │    │         ▼                           ▼          │
└──────────────┘    │  ┌─────────────┐           ┌─────────────┐     │
       │            │  │ Deepgram    │           │ Deepgram    │     │
       │            │  │ Flux (STT)  │           │ Aura-2 (TTS)│     │
       └────────────┼──│ WebSocket   │           │ REST API    │     │
         Audio      │  └─────────────┘           └─────────────┘     │
                    └─────────────────────────────────────────────────┘
                                       │
                                       │ ngrok tunnel
                                       ▼
                                   Internet
```

### Flow

1. Caller dials your Twilio number
2. Twilio streams audio to `deepclaw` server via WebSocket
3. Audio goes to Flux, which returns transcripts + `EndOfTurn` events
4. On `EndOfTurn`, send transcript to OpenClaw's `/v1/chat/completions`
5. OpenClaw response → Aura-2 TTS → audio back through Twilio to caller

Flux's native turn detection drives the conversation. No separate VAD layer.

## Barge-In Handling

### State Machine

```
                    ┌──────────────────────────────────┐
                    │                                  │
                    ▼                                  │
┌─────────┐    ┌─────────┐    ┌─────────┐    ┌────────┴──┐
│  IDLE   │───▶│LISTENING│───▶│THINKING │───▶│ SPEAKING  │
└─────────┘    └─────────┘    └─────────┘    └───────────┘
                    ▲                              │
                    │         StartOfTurn          │
                    └──────────────────────────────┘
                           (barge-in: stop TTS)
```

### States

- `IDLE` - Waiting for call
- `LISTENING` - Streaming audio to Flux, waiting for `EndOfTurn`
- `THINKING` - Sent to OpenClaw, waiting for response
- `SPEAKING` - Playing Aura-2 audio to caller

### Barge-In Logic

```python
if state == SPEAKING and flux_event == "StartOfTurn":
    stop_tts_playback()      # Tell Twilio to stop playing
    clear_audio_buffer()     # Discard queued TTS chunks
    state = LISTENING        # Back to listening
```

Use Twilio WebSocket `clear` message to halt playback mid-stream.

## Project Structure

```
deepclaw/
├── deepclaw/
│   ├── __init__.py
│   ├── server.py          # FastAPI app, Twilio webhook endpoints
│   ├── flux_client.py     # Deepgram Flux WebSocket handling
│   ├── tts.py             # Aura-2 TTS streaming
│   ├── openclaw.py        # OpenClaw chat/completions client
│   └── state.py           # Call state machine
├── README.md
├── pyproject.toml
└── .env.example           # DEEPGRAM_API_KEY, TWILIO_*, OPENCLAW_*
```

### Dependencies

- `fastapi` + `uvicorn` - HTTP server for Twilio webhooks
- `websockets` - Flux streaming
- `httpx` - Aura-2 and OpenClaw REST calls
- `python-dotenv` - Config

### Endpoints

- `POST /twilio/incoming` - Twilio calls this when someone dials in, returns TwiML to start media stream
- `WS /twilio/media` - Bidirectional audio stream with Twilio

## Error Handling

| Scenario | Handling |
|----------|----------|
| Flux WebSocket drops | Reconnect with exponential backoff, hold call in LISTENING state |
| OpenClaw timeout/error | TTS fallback: "Sorry, I couldn't process that. Try again." |
| Aura-2 fails | Same fallback, log error |
| Caller hangs up mid-TTS | Clean up state, close Flux connection |
| Twilio webhook auth fails | Validate `X-Twilio-Signature` header, reject spoofed requests |
| ngrok tunnel dies | Call fails - user restarts (document clearly) |

### Timeouts

- OpenClaw response: 30s max
- Aura-2 TTFB: 500ms warning log, 2s hard fail
- Flux silence fallback: configurable via `eot_silence_threshold_ms` (default 5s)

### Logging

- Log state transitions: `LISTENING → THINKING → SPEAKING`
- Log latencies: Flux EndOfTurn → OpenClaw response → Aura-2 first byte
- These become proof points for "faster than ElevenLabs"

## Messaging

### Tweet

> "ElevenLabs showed you how to call your OpenClaw. Here's how to do it 2x faster, 40% cheaper, with real barge-in support.
>
> deepclaw: Deepgram Flux + Aura-2 + OpenClaw
>
> Open source. Self-hostable. Ship it today. 🦞"

### Coming Soon (tease in README)

- Local wake-word mode (no phone needed)
- One-click desktop installer
- Native OpenClaw plugin

## Implementation Tasks

1. Set up project structure and dependencies
2. Implement Flux WebSocket client with StartOfTurn/EndOfTurn handling
3. Implement Aura-2 TTS streaming
4. Implement OpenClaw chat/completions client
5. Implement call state machine with barge-in
6. Build FastAPI server with Twilio webhook endpoints
7. Write README with setup instructions
8. Test end-to-end with real phone call
9. Record demo video
10. Ship it
