# H.E.R.A.L.D.

**Heuristic Entity for Real-time Audio Linkage and Distribution**

A self-hosted, AI-powered facility intercom system that turns Google Nest Mini speakers into nodes of a voice-activated assistant — powered by a local LLM with a SHODAN personality.

## How It Works

H.E.R.A.L.D. hijacks Google Meet calls to bridge audio between Nest Mini speakers and a central AI brain. When someone says the wake word near a speaker, the system listens, thinks, and responds — all processed locally.

```
  Nest Mini           Node (Docker)              Cortex                  Ollama
 +---------+     +------------------+     +------------------+     +-----------+
 | Speaker | <-> | Chrome + Meet    | <-> | Wake Word (OWW)  | --> | qwen2.5:3b|
 |         |     | PulseAudio TCP   |     | STT (Google)     |     | (SHODAN)  |
 +---------+     +------------------+     | TTS (XTTS v2)    |     +-----------+
                                          +------------------+
                    Audio over TCP:4713
```

1. **Nodes** join a Google Meet call to each Nest Mini using headless Chrome
2. **Cortex** taps into each node's audio stream via PulseAudio TCP
3. On hearing "Hey Jarvis", it records until silence is detected (WebRTC VAD)
4. Speech is transcribed, sent to the local LLM, and the response is spoken back through the Nest Mini using a voice-cloned TTS engine

## Architecture

| Component | Container | Role |
|---|---|---|
| **Mind** | `herald_mind` | Ollama instance running `qwen2.5:3b` with a SHODAN system prompt |
| **Nodes** | `herald_node01`, `...` | Headless Chrome containers that join Meet calls and expose audio via PulseAudio TCP |
| **Cortex** | `herald_cortex` | Central brain — wake word detection, STT, LLM queries, TTS, audio routing |

### Audio Pipeline

Each node runs a full PulseAudio stack with virtual sinks:

- `SpeakerOutput` — what Chrome plays into the Meet call (TTS output goes here)
- `VirtualMic` — remapped source from `VirtualMicSink.monitor` (what Chrome captures)
- `SpeakerOutput.monitor` — what Cortex records from (captures everything the Nest Mini hears)

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

Create a `.env` file with the Meet room URL for each node:

```
MEET_URL_01=https://meet.google.com/xxx-xxxx-xxx
```

### 4. Add a Voice Sample

Place a reference WAV file at `voice_sample/reference.wav`. This is used by XTTS v2 to clone the voice for TTS output.

### 5. Generate the Compose File (multi-node)

For deployments with multiple nodes, generate `docker-compose.yml` from your config:

```bash
python scripts/generate_deployment.py
```

### 6. Build and Run

```bash
docker-compose up --build -d
```

On first run, Ollama will pull the `qwen2.5:3b` model and Coqui will download XTTS v2 weights. These are cached in `ollama_data/` and `tts_cache/` respectively.

## Usage

### Health Check

```bash
curl http://localhost:5000/health
```

### Inject Speech

Send text to be spoken through a specific node:

```bash
curl -X POST http://localhost:5000/inject -d '{"node":"Node01", "text":"I see you."}'
```

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
| STT | Google Web Speech API via SpeechRecognition |
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

## Network

All containers communicate over a private `herald_net` bridge network. Only the Cortex health check endpoint (port 5000) is exposed to the host. PulseAudio TCP (port 4713) is internal only.
