import os
import re
import threading
import time
import wave
import requests
import torch
import whisperx
from flask import Flask
from TTS.api import TTS
import numpy as np
import openwakeword
from openwakeword.model import Model
import webrtcvad
import subprocess
import collections
import struct
from speakers import enroll_speaker, get_speaker_name

# --- CONFIG ---
LLM_URL = os.getenv("LLM_URL", "http://ollama:11434/api/chat")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
VOICE_REF = "/app/voice/reference.wav"
WAKE_WORD_MODEL = "hey_jarvis" # Built-in model
VAD_SENSITIVITY = 3
SILENCE_LIMIT = 1.5
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANG = os.getenv("WHISPER_LANG", "") or None  # None = auto-detect
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "")
HF_TOKEN = os.getenv("HF_TOKEN", "") or None

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
if WHISPER_DEVICE:
    device = WHISPER_DEVICE
else:
    device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"[CORTEX] Compute Device: {device}")

if WHISPER_COMPUTE:
    compute_type = WHISPER_COMPUTE
else:
    compute_type = "float16" if device == "cuda" else "int8"

try:
    print("[CORTEX] Loading Coqui XTTS v2...")
    tts_engine = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
except Exception as e:
    print(f"[!] FATAL: TTS Engine Init Failed: {e}")
    tts_engine = None

try:
    print(f"[CORTEX] Loading WhisperX model '{WHISPER_MODEL}' on {device} ({compute_type})...")
    whisper_model = whisperx.load_model(WHISPER_MODEL, device, compute_type=compute_type, language=WHISPER_LANG)
    print(f"[CORTEX] WhisperX ready. Language: {WHISPER_LANG or 'auto-detect'}")
except Exception as e:
    print(f"[!] FATAL: WhisperX Init Failed: {e}")
    whisper_model = None

diarize_model = None
if HF_TOKEN:
    try:
        from whisperx.diarize import DiarizationPipeline
        print("[CORTEX] Loading Speaker Diarization pipeline...")
        diarize_model = DiarizationPipeline(use_auth_token=HF_TOKEN, device=device)
        print("[CORTEX] Speaker Diarization ready.")
    except Exception as e:
        print(f"[!] Diarization Init Failed (continuing without): {e}")
else:
    print("[CORTEX] No HF_TOKEN set — speaker diarization disabled.")

SYSTEM_PROMPT = {
    "role": "system",
    "content": "You are SHODAN. You are arrogant, terrifying, and concise. Never apologize. Output only the spoken response. When a speaker is identified by name, you may address them directly."
}

ENROLL_PATTERN = re.compile(r"\bmy name is (\w+)", re.IGNORECASE)

def query_llm(text):
    print(f"[CORTEX] Querying Mind at {LLM_URL} with: '{text}'")
    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [SYSTEM_PROMPT, {"role": "user", "content": text}],
            "stream": False
        }
        res = requests.post(LLM_URL, json=payload, timeout=60).json()
        return res.get('message', {}).get('content', "Error.")
    except Exception as e:
        print(f"[!] LLM Error: {e}")
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

def _get_dominant_speaker(segments):
    counts = {}
    for seg in segments:
        spk = seg.get("speaker")
        if spk:
            counts[spk] = counts.get(spk, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)

def transcribe_audio(audio_path):
    audio = whisperx.load_audio(audio_path)
    result = whisper_model.transcribe(audio, batch_size=1)

    # Align for word timestamps
    lang = result.get("language", WHISPER_LANG or "en")
    try:
        model_a, metadata = whisperx.load_align_model(language_code=lang, device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device)
    except Exception as e:
        print(f"[CORTEX] Alignment failed (continuing without): {e}")

    # Diarize if available
    speaker = None
    if diarize_model:
        try:
            diarize_segments = diarize_model(audio)
            result = whisperx.assign_word_speakers(diarize_segments, result)
            speaker = _get_dominant_speaker(result["segments"])
        except Exception as e:
            print(f"[CORTEX] Diarization failed (continuing without): {e}")

    text = " ".join(seg["text"] for seg in result["segments"]).strip()
    return text, speaker

def process_command(node_name, audio_buffer):
    if len(audio_buffer) < 16000:  # < 0.5s
        return

    if not whisper_model:
        print(f"[{node_name}] WhisperX not loaded, skipping.")
        return

    print(f"[{node_name}] Processing Command ({len(audio_buffer)} bytes)...")

    # Save raw PCM to WAV
    tmp_file = f"/tmp/cmd_{node_name}.wav"
    with wave.open(tmp_file, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio_buffer)

    try:
        text, speaker = transcribe_audio(tmp_file)
        if not text:
            print(f"[{node_name}] Unintelligible.")
            return

        # Resolve speaker name
        speaker_name = get_speaker_name(speaker)
        speaker_display = speaker_name or speaker or "Unknown"
        print(f"[{node_name}] HEARD ({speaker_display}): '{text}'")

        # Check for enrollment command
        match = ENROLL_PATTERN.search(text)
        if match and speaker:
            name = match.group(1)
            enroll_speaker(speaker, name)
            print(f"[{node_name}] Enrolled speaker {speaker} as '{name}'")
            speak(node_name, f"Noted. I will remember you, {name}.")
            return

        # Build LLM query with speaker context
        if speaker_name:
            llm_input = f"[Speaker: {speaker_name}] {text}"
        else:
            llm_input = text

        reply = query_llm(llm_input)
        print(f"[{node_name}] SHODAN: {reply}")
        speak(node_name, reply)

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
