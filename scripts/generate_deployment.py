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
      # Generic paths inside container, mapped via volumes
      - COOKIE_FILE=/app/cookies/auth.txt{novnc_env}
    volumes:
      # Mount the specific cookie file for this node
      - ./cookies/{cookie_file}:/app/cookies/auth.txt
      # Persistent Chrome Profile for Red Phone Logic (Unique Volume)
      - herald_chrome_profile_{id}:/app/chrome_profile
    expose: ["4713"]{novnc_ports}
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
            novnc_env = "\n      - NOVNC_ENABLED=1"
            novnc_ports = f"\n    ports:\n      - \"127.0.0.1:68{nid}:6080\""
        else:
            novnc_env = ""
            novnc_ports = ""
        services_yaml += NODE_TEMPLATE.format(
            id=nid,
            esp_ip=node.get('esp_ip', ''),
            meet_url=node.get('meet_url', 'https://meet.google.com/calling/'),
            cookie_file=node['cookie_file'],
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
    parser.add_argument("--novnc", action="store_true", help="Enable noVNC on port 680<id> per node")
    args = parser.parse_args()
    generate(novnc=args.novnc)
