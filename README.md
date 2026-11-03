# H.E.R.A.L.D.

> **Note:** This project is a prototype and a work in progress. Expect rough edges, breaking changes, and incomplete features.

**Heuristic Entity for Real-time Audio Linkage and Distribution**

A self-hosted, AI-powered facility intercom system that turns Google Nest Mini speakers into nodes of a voice-activated assistant — powered by a local LLM with a SHODAN personality.

## How It Works

H.E.R.A.L.D. hijacks Google Meet calls to bridge audio between Nest Mini speakers and a central AI brain. When someone says the wake word near a speaker, the system listens, thinks, and responds — all processed locally.

```
  Nest Mini           Node (Docker)              Cortex                  Ollama
 +---------+     +------------------+     +------------------+     +-----------+
 | Speaker | <-> | Chrome + Meet    | <-> | Wake Word (OWW)  | --> | qwen2.5:3b|
 |         |     | PulseAudio TCP   |     | STT (WhisperX)   |     | (SHODAN)  |
 +---------+     +------------------+     | TTS (XTTS v2)    |     +-----------+
                                          | Speaker ID       |
                                          +------------------+
                    Audio over TCP:4713
```

1. **Nodes** join a Google Meet call to each Nest Mini using headless Chrome
2. **Cortex** taps into each node's audio stream via PulseAudio TCP
3. On hearing "Hey Jarvis", it records until silence is detected (WebRTC VAD)
4. Speech is transcribed locally via WhisperX, optionally identifying the speaker by voice embedding
5. The transcript (with speaker context and a short per-node conversation history) is sent to the local LLM, and the response is spoken back through the Nest Mini using a voice-cloned TTS engine, in the language that was detected

## Architecture

| Component | Container | Role |
|---|---|---|
| **Mind** | `herald_mind` | Ollama instance running `qwen2.5:3b` with a SHODAN system prompt |
| **Nodes** | `herald_node01`, `...` | Headless Chrome containers that join Meet calls and expose audio via PulseAudio TCP |
| **Cortex** | `herald_cortex` | Central brain — wake word detection, STT (WhisperX), speaker identification, LLM queries, TTS, audio routing |

### Audio Pipeline

Each node runs a full PulseAudio stack with virtual sinks:

- `SpeakerOutput` — default sink: Chrome plays the remote side of the call (the Nest Mini's microphone) here
- `SpeakerOutput.monitor` — what Cortex records from (everything the Nest Mini hears)
- `VirtualMicSink` — Cortex plays TTS here (`paplay --device=VirtualMicSink`, override with `TTS_SINK`)
- `VirtualMic` — remapped source from `VirtualMicSink.monitor`; Chrome's microphone, so TTS goes into the call

Keeping TTS off `SpeakerOutput` means Cortex never records (and wakes on) its own voice. While a command is being processed, the node's incoming audio is drained and ignored.

Audio is tunneled between containers over TCP port 4713 using `module-native-protocol-tcp`.

## Prerequisites

- Docker and Docker Compose
- A Google account with access to Google Meet
- Exported Google session cookies for each node (Netscape cookie format)
- A voice sample WAV file for TTS cloning
- NVIDIA GPU recommended (CPU fallback available but slow for TTS)

## Project Structure

```
herald/
+-- docker-compose.yml         # Service orchestration
+-- herald_nodes.json          # Node deployment configuration
+-- .env                       # Meet room URLs
+-- cortex/
|   +-- core.py                # Central brain (wake word -> STT -> LLM -> TTS)
|   +-- speakers.py            # Speaker enrollment persistence
|   +-- Dockerfile
|   +-- requirements.txt
+-- nodes/
|   +-- agent.py               # Browser automation (joins/monitors Meet calls)
|   +-- entrypoint.sh          # PulseAudio + Xvfb bootstrap
|   +-- Dockerfile
|   +-- daemon.conf            # PulseAudio daemon config
|   +-- client.conf            # PulseAudio client config
+-- scripts/
|   +-- generate_deployment.py # Generates docker-compose.yml from herald_nodes.json
+-- cookies/                   # Google session cookies (per node)
+-- voice_sample/
|   +-- reference.wav          # Voice clone source for TTS
+-- tts_cache/                 # Persisted Coqui XTTS v2 model files
+-- ollama_data/               # Persisted Ollama model weights
+-- whisper_cache/             # Persisted model cache (WhisperX, alignment, pyannote)
+-- speaker_data/              # Speaker enrollment data (speakers.json)
```

## Setup

### 1. Configure Nodes

Edit `herald_nodes.json` to define your nodes:

```json
[
    {
        "id": "01",
        "cookie_file": "herald_node01.txt",
        "esp_ip": "192.168.1.155",
        "meet_url": "https://meet.google.com/calling/"
    }
]
```

### 2. Add Session Cookies

Export your Google account cookies in Netscape format and place them in `cookies/`:

```
cookies/herald_node01.txt
cookies/herald_node02.txt
```

### 3. Set Environment Variables

Create a `.env` file with the Meet room URL for each node (it overrides `meet_url` from `herald_nodes.json`):

```
MEET_URL_01=https://meet.google.com/xxx-xxxx-xxx
```

On startup each node loads its cookie file into Chrome (via CDP) before opening Meet, so a fresh profile volume logs in without manual steps.

Google invalidates exported session cookies after a while. When that happens the node logs `Google session invalid (redirected to login)` and retries every `LOGIN_RETRY_SECONDS` (default 300), re-reading the cookie file each time: export fresh cookies over `cookies/herald_nodeXX.txt` and the node picks them up without a rebuild.

### 4. Add a Voice Sample

Place a reference WAV file at `voice_sample/reference.wav`. This is used by XTTS v2 to clone the voice for TTS output.

### 5. Generate the Compose File (multi-node)

For deployments with multiple nodes, generate `docker-compose.yml` from your config:

```bash
python scripts/generate_deployment.py
```

`docker-compose.yml` is generated by this script: change the templates there, not the compose file.

### 6. Build and Run

**With the bundled Ollama container:**

```bash
docker compose --profile local-llm up --build -d
```

On first run, Ollama will pull the `qwen2.5:3b` model and Coqui will download XTTS v2 weights. These are cached in `ollama_data/` and `tts_cache/` respectively.

**With an external LLM:** see [Using an External LLM](#using-an-external-llm) below.

## Using an External LLM

By default, H.E.R.A.L.D. ships with a bundled Ollama container. You can skip it entirely and point Cortex at any Ollama-compatible API instead.

Set `LLM_URL` and `LLM_MODEL` in your `.env` file:

```
LLM_URL=http://192.168.1.100:11434/api/chat
LLM_MODEL=llama3:8b
```

Then start without the `local-llm` profile — the Ollama container won't be created:

```bash
docker compose up --build -d
```

| Mode | Command | `.env` vars needed? |
|---|---|---|
| Bundled Ollama | `docker compose --profile local-llm up` | No (defaults work) |
| External LLM | `docker compose up` | Yes — `LLM_URL` and `LLM_MODEL` |

The external endpoint must expose the [Ollama `/api/chat`](https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completion) interface. Any server that implements it (Ollama, LiteLLM, LocalAI, etc.) will work.

## Speech Recognition (WhisperX)

Cortex uses [WhisperX](https://github.com/m-bain/whisperX) for fully local, offline speech-to-text. All transcription runs inside the container — no audio is sent to external services.

### Configuration

All settings are optional. Defaults work out of the box on CPU with English auto-detection.

| Variable | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `base` | Whisper model to load. Options: `tiny`, `base`, `small`, `medium`, `large-v3`, or any HuggingFace model ID (e.g., `Berly00/whisper-small-portuguese-1`) |
| `WHISPER_LANG` | *(auto-detect)* | Force a language code (e.g., `pt`, `en`, `es`). Leave empty for auto-detection. |
| `WHISPER_DEVICE` | *(auto)* | Force `cuda` or `cpu`. Leave empty to auto-detect via `torch.cuda.is_available()`. |
| `WHISPER_COMPUTE` | *(auto)* | Compute type: `float16` (GPU), `int8` (CPU), or `float32`. Auto-selects based on device if empty. |

Example `.env` for Portuguese on GPU:

```
WHISPER_MODEL=small
WHISPER_LANG=pt
WHISPER_DEVICE=cuda
WHISPER_COMPUTE=float16
```

The non-suffixed models (`base`, `small`, etc.) are multilingual and support Portuguese, English, and 90+ other languages. The `.en` suffixed models (e.g., `base.en`) are English-only but slightly more accurate for English.

WhisperX models (and the per-language alignment models) are downloaded on first run and cached in `whisper_cache/`.

### Spoken language

TTS replies in the language Whisper detected when XTTS supports it (`pt`, `en`, `es`, …), otherwise English. Set `TTS_LANG=pt` to force one.

## Speaker Identification

Cortex can identify **who** is speaking by comparing a voice embedding of each command (pyannote [`wespeaker-voxceleb-resnet34-LM`](https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM)) with the enrolled voices. Diarization labels (`SPEAKER_00`, …) are not used: they are only valid inside a single recording, so they cannot remember anyone.

| Variable | Default | Description |
|---|---|---|
| `SPEAKER_ID` | `1` if `HF_TOKEN` is set, else `0` | Enable voice identification |
| `HF_TOKEN` | *(empty)* | HuggingFace token, used to download the embedding model |
| `SPEAKER_EMBED_MODEL` | `pyannote/wespeaker-voxceleb-resnet34-LM` | pyannote embedding model |
| `SPEAKER_THRESHOLD` | `0.6` | Minimum cosine similarity to accept a match |

### Speaker Enrollment

Say:

> "Hey Jarvis, my name is John" (or "meu nome é João" / "me chamo João")

Cortex stores the voice embedding under `John` in `speaker_data/speakers.json`. Repeating the phrase averages a new sample into the stored voice, which improves recognition. On later commands the speaker's name is passed to the LLM. Files from the old label-based format are ignored and replaced on the next enrollment.

## Conversation Memory

Each node keeps the last `HISTORY_TURNS` (default 6) exchanges with the LLM, forgotten after `HISTORY_TTL` seconds (default 300) of silence. A single command is capped at `MAX_COMMAND_SECONDS` (default 15). After the wake word Cortex waits up to `COMMAND_START_TIMEOUT` seconds (default 3) for speech to start, then ends the command after 1.5 s of silence. Silence is detected by WebRTC VAD in mode `VAD_MODE` (default 2; mode 3 rejects ordinary speech and cuts commands short).

## Usage

### Health Check

```bash
curl http://localhost:5000/health
```

### Inject Speech

Send text to be spoken through a specific node (returns `202` and speaks asynchronously; optional `"lang":"pt"`):

```bash
curl -X POST http://localhost:5000/inject -d '{"node":"Node01", "text":"I see you."}'
```

Errors: `400` missing `node`/`text`, `404` unknown node, `503` TTS not loaded.

### View Logs

```bash
docker logs -f herald_cortex    # Brain activity
docker logs -f herald_node01    # Node/browser activity
docker logs -f herald_mind      # LLM inference
```

### Restart a Node's Audio

If PulseAudio drops on a node:

```bash
docker exec herald_node01 pulseaudio -k && docker restart herald_node01
```

## Tech Stack

| Layer | Technology |
|---|---|
| LLM | Ollama + Qwen 2.5 3B |
| TTS | Coqui XTTS v2 (voice cloning) |
| STT | WhisperX (local, offline) |
| Speaker ID | pyannote-audio via WhisperX (optional, requires HuggingFace token) |
| Wake Word | OpenWakeWord ("hey_jarvis" model, ONNX) |
| VAD | WebRTC VAD |
| Browser | undetected-chromedriver + Selenium |
| Audio | PulseAudio (TCP tunneling) |
| Display | Xvfb (virtual framebuffer) |
| Infra | Docker Compose |
| Hardware (optional) | ESP8266 D1 Mini for physical redial trigger |

## GPU Support

GPU acceleration is supported for both the LLM (Ollama) and TTS (Cortex) containers. Uncomment the `deploy.resources.reservations` blocks in `docker-compose.yml`:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

Requires the NVIDIA Container Toolkit to be installed on the host.


## Debugging with noVNC

Each node container runs headless Chrome on a virtual display (`Xvfb :99`). noVNC lets you view that display live in a browser — useful for debugging Selenium automation without needing a VNC client.

### How to enable

Generate the compose file with the `--novnc` flag:

```bash
python scripts/generate_deployment.py --novnc
```

Then start as normal:

```bash
docker compose up --build
```

### Port scheme

Each node exposes its noVNC interface on a unique host port derived from its node ID:

| Node | URL |
|---|---|
| `node01` | `http://localhost:6801/vnc.html` |
| `node02` | `http://localhost:6802/vnc.html` |

### How it works

1. `x11vnc` attaches to the Xvfb display (`:99`) and serves VNC on port `5900`
2. `websockify` proxies VNC → WebSocket on port `6080`, serving the noVNC static files
3. The host binds `127.0.0.1:680<id>` → container port `6080` (use an SSH tunnel to reach it remotely)

### Security note

noVNC has no password and no authentication. It is a development tool only. **Do not expose these ports publicly.**

## Network

All containers communicate over a private `herald_net` bridge network. Only the Cortex API (port 5000) is published, bound to `127.0.0.1` by default because `/inject` has no authentication; set `CORTEX_BIND=0.0.0.0` to expose it on the LAN. All services use `restart: unless-stopped`, so a crashed node or Cortex comes back on its own. PulseAudio TCP (port 4713) is internal only.

## Tests

Unit tests stub the heavy ML and browser dependencies, so they run without a GPU or Docker:

```bash
uv venv .venv && uv pip install --python .venv/bin/python flask numpy requests pytest
.venv/bin/python -m pytest -q tests/
```
