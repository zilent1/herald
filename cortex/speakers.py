import json
import os

SPEAKERS_FILE = os.getenv("SPEAKERS_FILE", "/app/data/speakers.json")


def _load():
    if os.path.exists(SPEAKERS_FILE):
        with open(SPEAKERS_FILE) as f:
            return json.load(f)
    return {}


def _save(data):
    os.makedirs(os.path.dirname(SPEAKERS_FILE), exist_ok=True)
    with open(SPEAKERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def enroll_speaker(label, name):
    data = _load()
    data[label] = name
    _save(data)


def get_speaker_name(label):
    if not label:
        return None
    data = _load()
    return data.get(label)
