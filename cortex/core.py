import os
import threading
import time
import requests
import torch
import speech_recognition as sr
from flask import Flask
from TTS.api import TTS
import numpy as np
import openwakeword
from openwakeword.model import Model
import webrtcvad
import subprocess
import collections
import struct

# --- CONFIG ---
OLLAMA_URL = "http://ollama:11434/api/chat"
MODEL = "qwen2.5:3b"
VOICE_REF = "/app/voice/reference.wav"
WAKE_WORD_MODEL = "hey_jarvis" # Built-in model
VAD_SENSITIVITY = 3
SILENCE_LIMIT = 1.5

# Definition: Name|Host|Port
NODES = {}
nodes_env = os.getenv("HERALD_NODES", "")
if nodes_env:
    for entry in nodes_env.split(","):
        if "|" in entry:
            name, host, port = entry.split("|")
            NODES[name] = {"host": host, "port": port}
else:
    print("[!][CORTEX] No HERALD_NODES env var found. Using default.")
    NODES = {"Node01": {"host": "herald_node01", "port": 4713}}

print(f"[CORTEX] Registered Nodes: {list(NODES.keys())}")

# --- INIT ---
print("[CORTEX] Initializing System...")
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[CORTEX] Compute Device: {device}")

try:
    print("[CORTEX] Loading Coqui XTTS v2...")
    tts_engine = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
except Exception as e:
    print(f"[!] FATAL: TTS Engine Init Failed: {e}")
    tts_engine = None

SYSTEM_PROMPT = {
    "role": "system", 
    "content": "You are SHODAN. You are arrogant, terrifying, and concise. Never apologize. Output only the spoken response."
}

def query_ollama(text):
    print(f"[CORTEX] Querying Mind with: '{text}'")
    try:
        payload = {
            "model": MODEL,
            "messages": [SYSTEM_PROMPT, {"role": "user", "content": text}],
            "stream": False
        }
        res = requests.post(OLLAMA_URL, json=payload, timeout=60).json()
        return res.get('message', {}).get('content', "Error.")
    except Exception as e:
        print(f"[!] Ollama Error: {e}")
        return "Cognitive failure."

def speak(node_name, text):
    if not tts_engine: return
    node = NODES.get(node_name)
    if not node: return
    
    wav_path = f"/tmp/out_{node_name}.wav"
    if os.path.exists(wav_path): os.remove(wav_path)

    try:
        print(f"[{node_name}] Generating Voice: '{text}'")
        tts_engine.tts_to_file(text=text, speaker_wav=VOICE_REF, language="en", file_path=wav_path)
        
        server = f"tcp:{node['host']}:{node['port']}"
        print(f"[{node_name}] Transmitting Audio to {server}...")
        os.system(f"PULSE_SERVER={server} paplay {wav_path}")
    except Exception as e:
        print(f"[{node_name}] Speech Error: {e}")

def process_command(node_name, audio_buffer):
    # Check if buffer has enough data
    if len(audio_buffer) < 16000: # < 0.5s
        return

    print(f"[{node_name}] Processing Command ({len(audio_buffer)} bytes)...")
    
    # Save to wav for SR
    import wave
    tmp_file = f"/tmp/cmd_{node_name}.wav"
    with wave.open(tmp_file, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio_buffer)
        
    # STT
    r = sr.Recognizer()
    try:
        with sr.AudioFile(tmp_file) as source:
            audio = r.record(source)
            text = r.recognize_google(audio)
            print(f"[{node_name}] HEARD: '{text}'")
            
            # Think & Speak
            reply = query_ollama(text)
            print(f"[{node_name}] SHODAN: {reply}")
            speak(node_name, reply)
            
    except sr.UnknownValueError:
        print(f"[{node_name}] Unintelligible.")
        # speak(node_name, "Speak up, insect.") 
    except Exception as e:
        print(f"[{node_name}] STT Error: {e}")

def listen_routine(node_name, host, port):
    print(f"[{node_name}] Initializing Air-Gapped Ear on tcp:{host}:{port}...")
    
    # Load Models (Thread-safe?)
    # OpenWakeWord seems efficiently implemented. 
    # Validating if we can instantiate multiple Models or if they share ONNX runtime.
    # It's safer to instantiate one per thread or use a shared one with lock.
    # Given the requested architecture, let's instantiate per thread for simplicity.
    try:
        ow_model = Model(wakeword_models=[WAKE_WORD_MODEL], inference_framework="onnx")
    except Exception as e:
        print(f"[{node_name}] Failed to load Wake Word Model: {e}")
        return

    vad = webrtcvad.Vad(VAD_SENSITIVITY)
    
    # Audio Stream via parecord
    cmd = ["parecord", "--rate=16000", "--channels=1", "--format=s16le", "--device=SpeakerOutput.monitor"]
    env = os.environ.copy()
    env["PULSE_SERVER"] = f"tcp:{host}:{port}"
    
    # Retry loop
    while True:
        try:
            print(f"[{node_name}] Connecting to Audio Stream...")
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            
            # Buffers
            CHUNK_SAMPLES = 1280
            CHUNK_BYTES = CHUNK_SAMPLES * 2
            
            is_active = False
            silence_start = time.time()
            command_buffer = bytearray()
            
            while True:
                data = process.stdout.read(CHUNK_BYTES)
                if not data or len(data) < CHUNK_BYTES:
                    print(f"[{node_name}] Stream ended/broken.")
                    break
                
                # numpy for Wake Word
                audio_np = np.frombuffer(data, dtype=np.int16)
                
                if not is_active:
                    # PASSIVE LISTENING
                    prediction = ow_model.predict(audio_np)
                    if prediction[WAKE_WORD_MODEL] > 0.5:
                        print(f"\n[{node_name}] >>> WAKE WORD DETECTED! <<<")
                        is_active = True
                        silence_start = time.time()
                        command_buffer = bytearray() # Clear older context
                        # Determine if we should keep the trigger audio? No, usually separate.
                else:
                    # ACTIVE RECORDING (VAD)
                    # VAD needs 10, 20, or 30ms.
                    # 16000Hz * 0.03s = 480 samples = 960 bytes.
                    # We have 1280 samples.
                    # Logic: We append to a small VAD buffer and process 30ms chunks.
                    
                    # Simple Approach: Just append current chunk to command buffer
                    command_buffer.extend(data)
                    
                    # Check for speech in this chunk (approximate)
                    # We scan sub-chunks
                    speech_found = False
                    for i in range(0, len(data), 960):
                        sub = data[i:i+960]
                        if len(sub) == 960:
                            if vad.is_speech(sub, 16000):
                                speech_found = True
                    
                    if speech_found:
                        silence_start = time.time()
                        # print("+", end="", flush=True)
                    else:
                        # print("-", end="", flush=True)
                        pass
                        
                    if (time.time() - silence_start) > SILENCE_LIMIT:
                        print(f"\n[{node_name}] Silence Detected. Processing Command...")
                        process_command(node_name, command_buffer)
                        is_active = False
                        command_buffer = bytearray()
                        
        except Exception as e:
            print(f"[{node_name}] Stream Error: {e}")
            if process: process.terminate()
            time.sleep(5)

def main():
    time.sleep(5)
    
    threads = []
    for n, info in NODES.items():
        t = threading.Thread(target=listen_routine, args=(n, info['host'], info['port']))
        t.daemon = True
        t.start()
        threads.append(t)
    
    app = Flask(__name__)
    @app.route('/health')
    def health(): return "Cortex Operational", 200
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    main()
