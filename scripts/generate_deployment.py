import json
import os

CONFIG_FILE = "herald_nodes.json"
OUTPUT_FILE = "docker-compose.yml"

BASE_OLLAMA = """
  # --- THE MIND ---
  ollama:
    image: ollama/ollama
    container_name: herald_mind
    restart: unless-stopped
    profiles: ["local-llm"]
    expose: ["11434"]
    volumes:
      - ./ollama_data:/root/.ollama
    # Auto-pull the model on start
    entrypoint: /bin/sh
    command: -c "ollama serve & sleep 5 && ollama pull qwen2.5:3b && wait"
    dns:
      - 8.8.8.8
      - 8.8.4.4
    networks: [herald_net]
"""

BASE_CORTEX = """
  # --- THE CORTEX ---
  cortex:
    build: ./cortex
    container_name: herald_cortex
    restart: unless-stopped
    volumes:
      # Mount the Voice Sample
      - ./voice_sample/reference.wav:/app/voice/reference.wav
      # Persist TTS Models
      - ./tts_cache:/root/.local/share/tts
      # Speaker enrollment data
      - ./speaker_data:/app/data
      # Model cache: WhisperX, torch alignment, pyannote
      - ./whisper_cache:/root/.cache
    ports: ["${{CORTEX_BIND:-127.0.0.1}}:5000:5000"]
    environment:
      - HERALD_NODES={herald_nodes_env}
      - LLM_URL=${{LLM_URL:-http://ollama:11434/api/chat}}
      - LLM_MODEL=${{LLM_MODEL:-qwen2.5:3b}}
      - WHISPER_MODEL=${{WHISPER_MODEL:-base}}
      - WHISPER_LANG=${{WHISPER_LANG:-}}
      - WHISPER_DEVICE=${{WHISPER_DEVICE:-}}
      - WHISPER_COMPUTE=${{WHISPER_COMPUTE:-}}
      - HF_TOKEN=${{HF_TOKEN:-}}
      - TTS_LANG=${{TTS_LANG:-}}
      - SPEAKER_ID=${{SPEAKER_ID:-}}
      - SPEAKER_THRESHOLD=${{SPEAKER_THRESHOLD:-0.6}}
      - HISTORY_TURNS=${{HISTORY_TURNS:-6}}
      - HISTORY_TTL=${{HISTORY_TTL:-300}}
      - MAX_COMMAND_SECONDS=${{MAX_COMMAND_SECONDS:-15}}
      - VAD_MODE=${{VAD_MODE:-2}}
      - COMMAND_START_TIMEOUT=${{COMMAND_START_TIMEOUT:-3}}
      - NODE_STATUS_PORT=8080
      - SELFTEST_STIMULUS=${{SELFTEST_STIMULUS:-}}
      - SELFTEST_RMS_MIN=${{SELFTEST_RMS_MIN:-200}}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 600s
      retries: 3
    depends_on:
{depends_on}
    networks: [herald_net]
"""

NODE_TEMPLATE = """
  # --- NODE {id} ---
  node{id}:
    build: ./nodes
    container_name: herald_node{id}
    restart: unless-stopped
    shm_size: "2gb"
    environment:
      - NODE_ID=Node{id}
      - ESP_IP={esp_ip}
      - MEET_URL=${{MEET_URL_{id}:-{meet_url}}}
{cookie_env}      - STATUS_PORT=8080
      - SESSION_CHECK_SECONDS=${{SESSION_CHECK_SECONDS:-1800}}
      - LOGIN_RETRY_SECONDS=${{LOGIN_RETRY_SECONDS:-300}}
      - COOKIE_INJECT=${{COOKIE_INJECT:-0}}
      - LOGIN_POLL_SECONDS=${{LOGIN_POLL_SECONDS:-10}}{novnc_env}
    volumes:
{cookie_volume}      # Persistent Chrome Profile for Red Phone Logic (Unique Volume)
      - herald_chrome_profile_{id}:/app/chrome_profile
    expose: ["4713", "8080"]{novnc_ports}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 30s
      retries: 3
    dns:
      - 8.8.8.8
      - 8.8.4.4
    networks: [herald_net]
"""

def generate(novnc=False):
    if not os.path.exists(CONFIG_FILE):
        print(f"[!] Config file {CONFIG_FILE} not found.")
        return

    with open(CONFIG_FILE, 'r') as f:
        nodes_config = json.load(f)

    services_yaml = ""
    volumes_yaml = "\nvolumes:\n"
    cortex_nodes_env = []
    node_ids = []

    # Generate Node Services
    for node in nodes_config:
        nid = node['id']
        node_ids.append(nid)
        # 1. Add Service Block
        if novnc:
            novnc_env = f"\n      - NOVNC_ENABLED=1\n      - NOVNC_URL=${{NOVNC_URL_{nid}:-http://127.0.0.1:68{nid}/vnc.html}}"
            novnc_ports = f"\n    ports:\n      - \"${{NOVNC_BIND:-127.0.0.1}}:68{nid}:6080\""
        else:
            novnc_env = ""
            novnc_ports = ""
        cookie_file = node.get('cookie_file')
        if cookie_file:
            cookie_env = f"      # Generic paths inside container, mapped via volumes\n      - COOKIE_FILE=/app/cookies/auth.txt\n"
            cookie_volume = f"      # Mount the specific cookie file for this node\n      - ./cookies/{cookie_file}:/app/cookies/auth.txt\n"
        else:
            cookie_env = ""
            cookie_volume = ""
        services_yaml += NODE_TEMPLATE.format(
            id=nid,
            esp_ip=node.get('esp_ip', ''),
            meet_url=node.get('meet_url', 'https://meet.google.com/calling/'),
            cookie_env=cookie_env,
            cookie_volume=cookie_volume,
            novnc_env=novnc_env,
            novnc_ports=novnc_ports
        )
        # 2. Add Volume Declaration
        volumes_yaml += f"  herald_chrome_profile_{nid}:\n"
        
        # 3. Add to Cortex Env (Format: Name|Host|Port)
        # Hostname in docker-compose is the service name (node01, node02...)
        # We mapped port 4713 internally.
        cortex_nodes_env.append(f"Node{nid}|node{nid}|4713")

    # Generate Cortex Block
    cortex_depends_on = "\n".join(f"      - node{nid}" for nid in node_ids)
    cortex_block = BASE_CORTEX.format(herald_nodes_env=",".join(cortex_nodes_env), depends_on=cortex_depends_on)

    # Combine All
    final_compose = f"""services:
{BASE_OLLAMA}
{services_yaml}
{cortex_block}

networks:
  herald_net:
    driver: bridge

{volumes_yaml}
"""

    with open(OUTPUT_FILE, 'w') as f:
        f.write(final_compose)
    
    print(f"[+] Generated {OUTPUT_FILE} with {len(nodes_config)} nodes.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--novnc", action="store_true", help="deprecated, noVNC is on by default")
    parser.add_argument("--no-novnc", action="store_true", help="Disable noVNC")
    args = parser.parse_args()
    generate(novnc=not args.no_novnc)
