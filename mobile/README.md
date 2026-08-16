# Sirious Mobile

Flutter Android client for Sirious **Phase 1** — real-time voice conversation over the Cloud Run WebSocket bridge.

## Repo layout

```text
Sirious/
├── backend/          # FastAPI + Gemini Live server
├── mobile/           # This Flutter app
├── doc.md
└── product_phases.md
```

The mobile app lives **alongside** `backend/`, not inside it.

## Architecture

```text
AudioCaptureService (16 kHz PCM)
        ↓
WebSocketClient  ←→  Cloud Run /ws  ←→  Gemini Live
        ↓
AudioPlaybackService (24 kHz PCM, flush on interrupted)
        ↓
SiriousSessionController (state + transcripts + latency)
        ↓
VoiceSessionScreen
```

Protocol: [`../backend/docs/websocket_protocol.md`](../backend/docs/websocket_protocol.md)

## Prerequisites

- Flutter SDK (stable)
- Android device or emulator with microphone
- Deployed Sirious backend reachable over HTTPS/WSS

## Run

```bash
cd mobile
flutter pub get
flutter run
```

Override the WebSocket URL:

```bash
flutter run --dart-define=WS_URL=wss://your-service.run.app/ws
```

## Permissions

Android requires:

- `INTERNET`
- `RECORD_AUDIO`

The app requests microphone permission when you tap **Start session**.

## Phase 1 features

- [x] WebSocket transport (binary PCM + JSON events)
- [x] Continuous mic streaming
- [x] Assistant audio playback queue
- [x] Playback flush on `interrupted`
- [x] Transcript fragment aggregation
- [x] Session state machine
- [x] Basic latency timestamps

## Not in this scaffold

- iOS build
- Auth / user accounts
- Persistent transcript storage
- Session resumption handling
- Background recording

See [`../product_phases.md`](../product_phases.md) for the full roadmap.
