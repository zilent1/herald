import os
import re
import tempfile
import threading
import time
import wave
import requests
import torch
import whisperx
from flask import Flask, request, jsonify
from TTS.api import TTS
import numpy as np
import openwakeword
from openwakeword.model import Model
import webrtcvad
import subprocess
import collections
import struct
import queue
from speakers import enroll_speaker, identify_speaker

# --- CONFIG ---
LLM_URL = os.getenv("LLM_URL", "http://ollama:11434/api/chat")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
VOICE_REF = "/app/voice/reference.wav"
WAKE_WORD_MODEL = "hey_jarvis" # Built-in model
VAD_MODE = int(os.getenv("VAD_MODE", "2"))
SILENCE_LIMIT = 1.5
COMMAND_START_TIMEOUT = float(os.getenv("COMMAND_START_TIMEOUT", "3"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANG = os.getenv("WHISPER_LANG", "") or None  # None = auto-detect
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "")
HF_TOKEN = os.getenv("HF_TOKEN", "") or None
TTS_SINK = os.getenv('TTS_SINK', 'VirtualMicSink')
MAX_COMMAND_SECONDS = float(os.getenv('MAX_COMMAND_SECONDS', '15'))
HISTORY_TURNS = int(os.getenv('HISTORY_TURNS', '6'))
HISTORY_TTL = float(os.getenv('HISTORY_TTL', '300'))
_history = {}
_history_lock = threading.Lock()

# --- Node state & event bus ---
NODE_STATE = {}
_state_lock = threading.Lock()
_listeners = {}  # node -> list of queue.Queue
_selftest_pending = {}  # node -> run dict
SELFTEST_SINK = os.getenv('SELFTEST_SINK', 'SelftestSink')
SELFTEST_STIMULUS = os.getenv('SELFTEST_STIMULUS', '')
SELFTEST_CACHE = os.getenv('SELFTEST_CACHE', '/app/data/selftest_stimulus.wav')
SELFTEST_WAKE_TEXT = os.getenv('SELFTEST_WAKE_TEXT', 'Hey Jarvis.')
SELFTEST_COMMAND_TEXT = os.getenv('SELFTEST_COMMAND_TEXT', 'What is two plus two?')
SELFTEST_KEYWORDS = [w.strip().lower() for w in os.getenv('SELFTEST_KEYWORDS', 'two,plus,dois,mais,four,2').split(',') if w.strip()]
SELFTEST_RMS_MIN = float(os.getenv('SELFTEST_RMS_MIN', '200'))
STREAM_STALE_SECONDS = float(os.getenv('STREAM_STALE_SECONDS', '10'))
NODE_STATUS_PORT = int(os.getenv('NODE_STATUS_PORT', '8080'))

def node_state(node_name):
    with _state_lock:
        if node_name not in NODE_STATE:
            NODE_STATE[node_name] = {
                'stream_connected': False,
                'last_audio_ts': 0.0,
                'busy': False,
                'last_wake_ts': 0.0,
                'last_command_ts': 0.0,
                'last_selftest': None,
            }
        return NODE_STATE[node_name]

def emit(node_name, stage, **data):
    event = {'stage': stage, 'ts': time.time(), **data}
    if stage == 'wake':
        node_state(node_name)['last_wake_ts'] = event['ts']
    if stage == 'transcribed':
        node_state(node_name)['last_command_ts'] = event['ts']
    with _state_lock:
        subs = list(_listeners.get(node_name, []))
    for q in subs:
        q.put_nowait(event)

def subscribe(node_name):
    q = queue.Queue()
    with _state_lock:
        _listeners.setdefault(node_name, []).append(q)
    return q

def unsubscribe(node_name, q):
    with _state_lock:
        subs = _listeners.get(node_name, [])
        if q in subs:
            subs.remove(q)

XTTS_LANGS = {'en','es','fr','de','it','pt','pl','tr','ru','nl','cs','ar','zh-cn','ja','hu','ko','hi'}

tts_lock = threading.Lock()
whisper_lock = threading.Lock()
_align_models = {}

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

SPEAKER_ID = os.getenv('SPEAKER_ID', '1' if HF_TOKEN else '0') == '1'
SPEAKER_EMBED_MODEL = os.getenv('SPEAKER_EMBED_MODEL', 'pyannote/wespeaker-voxceleb-resnet34-LM')
speaker_embedder = None
if SPEAKER_ID:
    try:
        from pyannote.audio import Model as EmbedModel, Inference
        try:
            m = EmbedModel.from_pretrained(SPEAKER_EMBED_MODEL, token=HF_TOKEN)  # pyannote.audio >= 4
        except TypeError:
            m = EmbedModel.from_pretrained(SPEAKER_EMBED_MODEL, use_auth_token=HF_TOKEN)  # pyannote.audio 3.x
        speaker_embedder = Inference(m, window='whole')
        speaker_embedder.to(torch.device(device))
        print("[CORTEX] Speaker embedding ready.")
    except Exception as e:
        print(f"[!] Speaker embedding init failed (continuing without): {e}")
        speaker_embedder = None

SYSTEM_PROMPT = {
    "role": "system",
    "content": "You are SHODAN. You are arrogant, terrifying, and concise. Never apologize. Output only the spoken response. When a speaker is identified by name, you may address them directly."
}

ENROLL_PATTERN = re.compile(r'\b(?:my name is|meu nome é|meu nome e|me chamo)\s+(\w+)', re.IGNORECASE)

def pick_tts_lang(detected=None) -> str:
    forced = os.getenv('TTS_LANG')
    if forced:
        return forced
    if detected == 'zh':
        return 'zh-cn'
    if detected in XTTS_LANGS:
        return detected
    return 'en'

def query_llm(node_name, text, use_history=True):
    print(f"[CORTEX] Querying Mind at {LLM_URL} with: '{text}'")
    try:
        if use_history:
            with _history_lock:
                entry = _history.setdefault(node_name, {'messages': [], 'last': 0.0})
                if time.time() - entry['last'] > HISTORY_TTL:
                    entry['messages'] = []
                messages = [SYSTEM_PROMPT] + entry['messages'] + [{"role": "user", "content": text}]
        else:
            messages = [SYSTEM_PROMPT, {"role": "user", "content": text}]

        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": False
        }
        res = requests.post(LLM_URL, json=payload, timeout=60)
        res.raise_for_status()
        res = res.json()
        reply = res.get('message', {}).get('content', "Error.")

        if use_history:
            with _history_lock:
                entry['messages'].append({"role": "user", "content": text})
                entry['messages'].append({"role": "assistant", "content": reply})
                entry['messages'] = entry['messages'][-(HISTORY_TURNS * 2):]
                entry['last'] = time.time()

        return reply
    except Exception as e:
        print(f"[!] LLM Error: {e}")
        return "Cognitive failure."

def speak(node_name, text, language='en', sink=None):
    if not tts_engine: return False
    node = NODES.get(node_name)
    if not node: return False

    device = sink or TTS_SINK
    fd, wav_path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)

    ok = False
    try:
        print(f"[{node_name}] Generating Voice: '{text}'")
        with tts_lock:
            tts_engine.tts_to_file(text=text, speaker_wav=VOICE_REF, language=language, file_path=wav_path)

        server = f"tcp:{node['host']}:{node['port']}"
        print(f"[{node_name}] Transmitting Audio to {server}...")
        env = os.environ.copy()
        env["PULSE_SERVER"] = server
        for attempt in range(3):
            try:
                result = subprocess.run(['paplay', f'--device={device}', wav_path], env=env, timeout=120)
                if result.returncode == 0:
                    ok = True
                    break
            except Exception as e:
                print(f"[{node_name}] paplay attempt {attempt+1} failed: {e}")
            time.sleep(1)
    except Exception as e:
        print(f"[{node_name}] Speech Error: {e}")
    finally:
        if os.path.exists(wav_path): os.remove(wav_path)

    emit(node_name, 'tts_sent', ok=ok, sink=device)
    return ok

def transcribe_audio(audio_path):
    with whisper_lock:
        audio = whisperx.load_audio(audio_path)
        result = whisper_model.transcribe(audio, batch_size=1)

        # Align for word timestamps
        lang = result.get("language", WHISPER_LANG or "en")
        try:
            if lang in _align_models:
                model_a, metadata = _align_models[lang]
            else:
                model_a, metadata = whisperx.load_align_model(language_code=lang, device=device)
                _align_models[lang] = (model_a, metadata)
            result = whisperx.align(result["segments"], model_a, metadata, audio, device)
        except Exception as e:
            print(f"[CORTEX] Alignment failed (continuing without): {e}")

        text = " ".join(seg["text"] for seg in result["segments"]).strip()
        return text, lang

def embed_speaker(audio_path):
    if speaker_embedder is None:
        return None
    try:
        return speaker_embedder(audio_path)
    except Exception as e:
        print(f"[CORTEX] Speaker embedding failed: {e}")
        return None

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
        run = _selftest_pending.get(node_name)
        text, lang = transcribe_audio(tmp_file)
        emit(node_name, 'transcribed', text=text, lang=lang)
        if not text:
            print(f"[{node_name}] Unintelligible.")
            return

        if run is not None:
            reply = query_llm(node_name, text, use_history=False)
            emit(node_name, 'llm', reply=reply)
            speak(node_name, reply, language=pick_tts_lang(lang), sink=None if run.get('audible') else SELFTEST_SINK)
            return

        embedding = embed_speaker(tmp_file)
        speaker_name = identify_speaker(embedding)
        speaker_display = speaker_name or "Unknown"
        print(f"[{node_name}] HEARD ({speaker_display}): '{text}'")

        # Check for enrollment command
        match = ENROLL_PATTERN.search(text)
        if match:
            if embedding is None:
                speak(node_name, 'Voice identification is offline. I cannot remember you.', language=pick_tts_lang(lang))
                return
            name = match.group(1).capitalize()
            enroll_speaker(name, embedding)
            print(f"[{node_name}] Enrolled speaker as '{name}'")
            speak(node_name, f"Noted. I will remember you, {name}.", language=pick_tts_lang(lang))
            return

        # Build LLM query with speaker context
        if speaker_name:
            llm_input = f"[Speaker: {speaker_name}] {text}"
        else:
            llm_input = text

        reply = query_llm(node_name, llm_input)
        emit(node_name, 'llm', reply=reply)
        print(f"[{node_name}] SHODAN: {reply}")
        speak(node_name, reply, language=pick_tts_lang(lang))

    except Exception as e:
        print(f"[{node_name}] STT Error: {e}")
    finally:
        node_state(node_name)['busy'] = False  # don't wait for the next audio burst in listen_routine

def listen_routine(node_name, host, port):
    st = node_state(node_name)
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

    vad = webrtcvad.Vad(VAD_MODE)
    
    # Audio Stream via parecord
    cmd = ["parecord", "--raw", "--latency-msec=100", "--rate=16000", "--channels=1", "--format=s16le", "--device=SpeakerOutput.monitor"]
    env = os.environ.copy()
    env["PULSE_SERVER"] = f"tcp:{host}:{port}"
    
    # Retry loop
    process = None
    while True:
        try:
            print(f"[{node_name}] Connecting to Audio Stream...")
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            
            # Buffers
            CHUNK_SAMPLES = 1280
            CHUNK_BYTES = CHUNK_SAMPLES * 2
            
            is_active = False
            silent_bytes = 0
            heard_speech = False
            command_buffer = bytearray()
            worker = None
            
            while True:
                data = process.stdout.read(CHUNK_BYTES)
                if not data or len(data) < CHUNK_BYTES:
                    print(f"[{node_name}] Stream ended/broken.")
                    break

                st['last_audio_ts'] = time.time()
                st['stream_connected'] = True

                # Check if worker is still processing
                if worker and worker.is_alive():
                    continue
                if worker and not worker.is_alive():
                    ow_model.reset()
                    worker = None
                    st['busy'] = False
                    print(f"[{node_name}] Listening again.")
                
                # numpy for Wake Word
                audio_np = np.frombuffer(data, dtype=np.int16)
                
                if not is_active:
                    # PASSIVE LISTENING
                    prediction = ow_model.predict(audio_np)
                    if prediction[WAKE_WORD_MODEL] > 0.5:
                        print(f"\n[{node_name}] >>> WAKE WORD DETECTED! <<<")
                        emit(node_name, 'wake')
                        is_active = True
                        ow_model.reset()
                        silent_bytes = 0
                        heard_speech = False
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
                        silent_bytes = 0
                        heard_speech = True
                    else:
                        silent_bytes += len(data)
                    
                    should_process = silent_bytes >= (SILENCE_LIMIT if heard_speech else COMMAND_START_TIMEOUT) * 16000 * 2
                    if len(command_buffer) >= MAX_COMMAND_SECONDS * 16000 * 2:
                        should_process = True
                    
                    if should_process:
                        print(f"\n[{node_name}] Silence/Max Detected. Processing Command...")
                        st['busy'] = True
                        worker = threading.Thread(target=process_command, args=(node_name, bytes(command_buffer)), daemon=True)
                        worker.start()
                        is_active = False
                        command_buffer = bytearray()
                        
        except Exception as e:
            print(f"[{node_name}] Stream Error: {e}")
        st['stream_connected'] = False
        # Stream ended or failed: reap parecord and back off before reconnecting
        if process:
            process.terminate()
            process.wait()
        time.sleep(5)

def main():
    time.sleep(5)
    
    threads = []
    for n, info in NODES.items():
        t = threading.Thread(target=listen_routine, args=(n, info['host'], info['port']))
        t.daemon = True
        t.start()
        threads.append(t)

    app.run(host='0.0.0.0', port=5000)

# --- Self-test ---

def get_stimulus():
    """Return path to stimulus WAV file, generating it if needed."""
    if SELFTEST_STIMULUS and os.path.exists(SELFTEST_STIMULUS):
        return SELFTEST_STIMULUS
    if os.path.exists(SELFTEST_CACHE):
        return SELFTEST_CACHE
    if tts_engine is None:
        raise RuntimeError("tts_engine is None and no stimulus file exists")
    with tts_lock:
        fd1, tmp1 = tempfile.mkstemp(suffix='.wav')
        os.close(fd1)
        tts_engine.tts_to_file(text=SELFTEST_WAKE_TEXT, speaker_wav=VOICE_REF, language='en', file_path=tmp1)
        fd2, tmp2 = tempfile.mkstemp(suffix='.wav')
        os.close(fd2)
        tts_engine.tts_to_file(text=SELFTEST_COMMAND_TEXT, speaker_wav=VOICE_REF, language='en', file_path=tmp2)
    os.makedirs(os.path.dirname(SELFTEST_CACHE), exist_ok=True)
    with wave.open(tmp1, 'rb') as w1:
        rate, width, channels = w1.getframerate(), w1.getsampwidth(), w1.getnchannels()
        wake_frames = w1.readframes(w1.getnframes())
    with wave.open(tmp2, 'rb') as w2:
        cmd_frames = w2.readframes(w2.getnframes())
    silence = b'\x00' * int(rate * 0.8) * channels * width
    with wave.open(SELFTEST_CACHE, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(wake_frames + silence + cmd_frames)
    os.remove(tmp1)
    os.remove(tmp2)
    return SELFTEST_CACHE


class MonitorRecorder:
    def __init__(self, node_name, source):
        self.node_name = node_name
        self.source = source
        self.max_rms = 0.0
        self._process = None
        self._thread = None

    def start(self):
        node = NODES[self.node_name]
        env = os.environ.copy()
        env['PULSE_SERVER'] = f"tcp:{node['host']}:{node['port']}"
        self._process = subprocess.Popen(
            ['parecord', '--raw', '--rate=16000', '--channels=1', '--format=s16le', f'--device={self.source}'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while True:
            data = self._process.stdout.read(3200)
            if not data:
                break
            x = np.frombuffer(data, dtype=np.int16).astype(np.float64)
            rms = float(np.sqrt(np.mean(x ** 2)))
            if rms > self.max_rms:
                self.max_rms = rms

    def stop(self):
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process.wait()
        if self._thread:
            self._thread.join(timeout=2)
        return self.max_rms


def evaluate(run):
    """Pure function: evaluate selftest run results. Returns (ok, stages)."""
    started = run['started']
    stream_age = run['stream_age']
    events = run['events']
    rms = run['rms']
    played = run['played']

    def _t_ms(event):
        if event is None:
            return None
        return int((event['ts'] - started) * 1000)

    stages = {}

    # stream
    if stream_age is None:
        stages['stream'] = {'ok': False, 't_ms': None, 'detail': 'no audio'}
    else:
        stages['stream'] = {'ok': stream_age <= STREAM_STALE_SECONDS, 't_ms': None, 'detail': f'last audio {stream_age:.1f}s ago'}

    # stimulus
    stages['stimulus'] = {'ok': played, 't_ms': None, 'detail': 'ok' if played else 'playback failed'}

    # wake
    wake_evt = next((e for e in events if e['stage'] == 'wake'), None)
    stages['wake'] = {'ok': wake_evt is not None, 't_ms': _t_ms(wake_evt), 'detail': 'ok' if wake_evt else 'no event'}

    # stt
    transcribed_evt = next((e for e in events if e['stage'] == 'transcribed'), None)
    if transcribed_evt is None:
        stages['stt'] = {'ok': False, 't_ms': None, 'detail': 'no event'}
    else:
        text = transcribed_evt.get('text', '')
        text_lower = text.lower()
        ok = bool(text) and any(kw in text_lower for kw in SELFTEST_KEYWORDS)
        stages['stt'] = {'ok': ok, 't_ms': _t_ms(transcribed_evt), 'detail': text}

    # llm
    llm_evt = next((e for e in events if e['stage'] == 'llm'), None)
    if llm_evt is None:
        stages['llm'] = {'ok': False, 't_ms': None, 'detail': 'no event'}
    else:
        reply = llm_evt.get('reply', '')
        ok = bool(reply) and reply != 'Cognitive failure.'
        stages['llm'] = {'ok': ok, 't_ms': _t_ms(llm_evt), 'detail': reply[:120]}

    # tts
    tts_evt = next((e for e in events if e['stage'] == 'tts_sent' and e.get('ok')), None)
    if tts_evt is None:
        stages['tts'] = {'ok': False, 't_ms': None, 'detail': 'no event'}
    else:
        ok = rms >= SELFTEST_RMS_MIN
        stages['tts'] = {'ok': ok, 't_ms': _t_ms(tts_evt), 'detail': f'paplay ok, rms {int(rms)}'}

    overall_ok = all(s['ok'] for s in stages.values())
    return overall_ok, stages


def run_selftest(node_name, audible=False, timeout=120.0):
    """Run a full pipeline self-test on a node."""
    st = node_state(node_name)
    stream_age = time.time() - st['last_audio_ts'] if st['last_audio_ts'] else None
    sink = TTS_SINK if audible else SELFTEST_SINK
    q = subscribe(node_name)
    run = {'audible': audible}
    _selftest_pending[node_name] = run
    recorder = MonitorRecorder(node_name, f'{sink}.monitor')
    events = []
    started = time.time()
    played = False

    try:
        recorder.start()
        stimulus = get_stimulus()
        node = NODES[node_name]
        env = os.environ.copy()
        env['PULSE_SERVER'] = f"tcp:{node['host']}:{node['port']}"
        try:
            played = subprocess.run(['paplay', '--device=SpeakerOutput', stimulus], env=env, timeout=60).returncode == 0
        except Exception:
            played = False

        if played:
            deadline = time.time() + timeout  # count from end of playback (first run also generates the stimulus)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    evt = q.get(timeout=remaining)
                except queue.Empty:
                    break
                events.append(evt)
                if evt['stage'] == 'tts_sent':
                    break
                if evt['stage'] == 'transcribed' and not evt.get('text'):
                    break
    finally:
        rms = recorder.stop()
        unsubscribe(node_name, q)
        _selftest_pending.pop(node_name, None)

    ok, stages = evaluate({
        'started': started,
        'stream_age': stream_age,
        'events': events,
        'rms': rms,
        'played': played,
    })
    result = {
        'ok': ok,
        'node': node_name,
        'audible': audible,
        'started': started,
        'duration_ms': int((time.time() - started) * 1000),
        'stages': stages,
    }
    st['last_selftest'] = result
    return result


def fetch_node_status(node_name):
    host = NODES[node_name]['host']
    try:
        r = requests.get(f'http://{host}:{NODE_STATUS_PORT}/status', timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'ok': False, 'error': str(e), 'unreachable': True}

def collect_status():
    now = time.time()
    nodes = {}
    for name in NODES:
        st = dict(node_state(name))
        age = round(now - st['last_audio_ts'], 1) if st['last_audio_ts'] else None
        stream_ok = st['stream_connected'] and age is not None and age <= STREAM_STALE_SECONDS
        agent = fetch_node_status(name)
        nodes[name] = {
            'stream_connected': st['stream_connected'],
            'stream_age_s': age,
            'stream_ok': stream_ok,
            'busy': st['busy'],
            'last_wake_ts': st['last_wake_ts'] or None,
            'last_command_ts': st['last_command_ts'] or None,
            'last_selftest': st['last_selftest'],
            'agent': agent,
            'ok': bool(stream_ok and agent.get('ok')),
        }
    engines = {
        'tts': tts_engine is not None,
        'whisper': whisper_model is not None,
        'speaker_id': speaker_embedder is not None,
    }
    nodes_ok = all(n['ok'] for n in nodes.values()) if nodes else False
    return {
        'ok': bool(nodes_ok and engines['tts'] and engines['whisper']),
        'engines': engines,
        'nodes': nodes,
    }

# Flask app at module level
app = Flask(__name__)
app.json.sort_keys = False  # keep selftest stages in pipeline order

@app.route('/health')
def health():
    return "Cortex Operational", 200

@app.route('/status')
def status():
    return jsonify(collect_status()), 200

@app.route('/inject', methods=['POST'])
def inject():
    data = request.get_json(force=True, silent=True) or {}
    node = data.get('node')
    text = data.get('text')
    lang = data.get('lang')
    if not node or not text:
        return jsonify(error='missing node or text'), 400
    if node not in NODES:
        return jsonify(error='unknown node'), 404
    if tts_engine is None:
        return jsonify(error='tts engine unavailable'), 503
    threading.Thread(target=speak, args=(node, text, pick_tts_lang(lang)), daemon=True).start()
    return jsonify(status='queued'), 202

@app.route('/selftest', methods=['POST'])
def selftest():
    data = request.get_json(force=True, silent=True) or {}
    node = data.get('node')
    if not node:
        return jsonify(error='missing node'), 400
    if node not in NODES:
        return jsonify(error='unknown node'), 404
    if tts_engine is None or whisper_model is None:
        return jsonify(error='engine unavailable'), 503
    if node_state(node)['busy'] or node in _selftest_pending:
        return jsonify(error='busy'), 409
    audible = data.get('audible', False)
    timeout = data.get('timeout', 120.0)
    result = run_selftest(node, audible=audible, timeout=timeout)
    return jsonify(result), 200 if result['ok'] else 500

if __name__ == "__main__":
    main()
