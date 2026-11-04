import json
import os
import tempfile
import threading

import numpy as np

SPEAKERS_FILE = os.getenv("SPEAKERS_FILE", "/app/data/speakers.json")
SPEAKER_THRESHOLD = float(os.getenv("SPEAKER_THRESHOLD", "0.6"))

_lock = threading.Lock()


def _normalize(v):
    n = np.linalg.norm(v)
    if n == 0:
        return np.zeros_like(v)
    return v / n


def _load():
    path = SPEAKERS_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    speakers = data.get("speakers")
    if not isinstance(speakers, dict):
        return {}
    result = {}
    for name, vec in speakers.items():
        if isinstance(vec, list) and all(isinstance(x, (int, float)) for x in vec):
            result[name] = vec
    return result


def _save(registry):
    path = SPEAKERS_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"speakers": registry}, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def enroll_speaker(name: str, embedding) -> None:
    vec = np.array(embedding, dtype=np.float64)
    vec = _normalize(vec)
    with _lock:
        registry = _load()
        if name in registry:
            existing = _normalize(np.array(registry[name], dtype=np.float64))
            vec = _normalize(vec + existing)
        registry[name] = vec.tolist()
        _save(registry)


def identify_speaker(embedding, threshold: float | None = None) -> str | None:
    if embedding is None:
        return None
    vec = np.array(embedding, dtype=np.float64)
    if np.linalg.norm(vec) == 0:
        return None
    vec = _normalize(vec)
    if threshold is None:
        threshold = SPEAKER_THRESHOLD
    with _lock:
        registry = _load()
    if not registry:
        return None
    best_name = None
    best_sim = -1.0
    for name, stored in registry.items():
        if len(stored) != len(vec):
            continue
        stored_vec = _normalize(np.array(stored, dtype=np.float64))
        sim = float(np.dot(vec, stored_vec))
        if sim > best_sim:
            best_sim = sim
            best_name = name
    if best_name is not None and best_sim >= threshold:
        return best_name
    return None


def list_speakers() -> list[str]:
    with _lock:
        registry = _load()
    return list(registry.keys())
