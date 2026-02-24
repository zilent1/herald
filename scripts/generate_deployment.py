import json
import os

CONFIG_FILE = "herald_nodes.json"
OUTPUT_FILE = "docker-compose.yml"

BASE_OLLAMA = """
  # --- THE MIND ---
  ollama:
    image: ollama/ollama
    container_name: herald_mind
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
    volumes:
      # Mount the Voice Sample
      - ./voice_sample/reference.wav:/app/voice/reference.wav
      # Mount Server Cookies (Optional/Future Use)
      - ./cookies/herald_server.txt:/app/cookies/herald_server.txt
      # Persist TTS Models
      - ./tts_cache:/root/.local/share/tts
      # Speaker enrollment data
      - ./speaker_data:/app/data
      # WhisperX / HuggingFace model cache
      - ./whisper_cache:/root/.cache/huggingface
    ports: ["5000:5000"]
    environment:
      - HERALD_NODES={herald_nodes_env}
      - LLM_URL=${{LLM_URL:-http://ollama:11434/api/chat}}
      - LLM_MODEL=${{LLM_MODEL:-qwen2.5:3b}}
      - WHISPER_MODEL=${{WHISPER_MODEL:-base}}
      - WHISPER_LANG=${{WHISPER_LANG:-}}
      - WHISPER_DEVICE=${{WHISPER_DEVICE:-}}
      - WHISPER_COMPUTE=${{WHISPER_COMPUTE:-}}
      - HF_TOKEN=${{HF_TOKEN:-}}
    networks: [herald_net]
"""

NODE_TEMPLATE = """
  # --- NODE {id} ---
  node{id}:
    build: ./nodes
    container_name: herald_node{id}
    environment:
      - NODE_ID=Node{id}
      - ESP_IP={esp_ip}
      - MEET_URL={meet_url}
      # Generic paths inside container, mapped via volumes
      - COOKIE_FILE=/app/cookies/auth.txt{novnc_env}
    volumes:
      # Mount the specific cookie file for this node
      - ./cookies/{cookie_file}:/app/cookies/auth.txt
      # Persistent Chrome Profile for Red Phone Logic (Unique Volume)
      - herald_chrome_profile_{id}:/app/chrome_profile
      - /dev/shm:/dev/shm
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

    # Generate Node Services
    for node in nodes_config:
        nid = node['id']
        # 1. Add Service Block
        if novnc:
            novnc_env = "\n      - NOVNC_ENABLED=1"
            novnc_ports = f"\n    ports:\n      - \"68{nid}:6080\""
        else:
            novnc_env = ""
            novnc_ports = ""
        services_yaml += NODE_TEMPLATE.format(
            id=nid,
            esp_ip=node['esp_ip'],
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
    cortex_block = BASE_CORTEX.format(herald_nodes_env=",".join(cortex_nodes_env))
    
    # Add Dependencies to Cortex
    # We need to inject depend_on for all nodes to ensure they start before Cortex attempts to connect?
    # Actually, Cortex logic handles connection failures, so depends_on isn't strictly necessary for logic, 
    # but good for startup order.
    # Let's modify BASE_CORTEX slightly or just append them. 
    # For simplicity, we keep simple depends_on [ollama] for now.

    # Combine All
    final_compose = f"""version: '3.8'
services:
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
