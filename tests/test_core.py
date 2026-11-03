import sys
import os
import json
import tempfile
from unittest.mock import MagicMock

# --- Stub heavy dependencies BEFORE importing core ---
sys.modules['torch'] = MagicMock()
sys.modules['torch'].cuda.is_available.return_value = False
sys.modules['whisperx'] = MagicMock()
sys.modules['TTS'] = MagicMock()
sys.modules['TTS.api'] = MagicMock()
sys.modules['openwakeword'] = MagicMock()
sys.modules['openwakeword.model'] = MagicMock()
sys.modules['webrtcvad'] = MagicMock()

# Point SPEAKERS_FILE to a temp path before import
os.environ['SPEAKERS_FILE'] = tempfile.mktemp(suffix='.json')
# Ensure TTS_LANG is not set for pick_tts_lang tests
os.environ.pop('TTS_LANG', None)

import core


# ---------------------------------------------------------------------------
# /inject endpoint
# ---------------------------------------------------------------------------

class _FakeThread:
    """Thread substitute that executes target immediately on start()."""
    def __init__(self, target=None, args=(), daemon=False, **kwargs):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        if self.target:
            self.target(*self.args)


def test_inject_success(monkeypatch):
    """POST /inject with valid node+text (no content-type) -> 202, speak called."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core.threading, 'Thread', _FakeThread)
    monkeypatch.setattr(core, 'speak', MagicMock())

    app = core.app.test_client()
    resp = app.post(
        '/inject',
        data='{"node":"Node01","text":"hi"}',
        content_type=None,
    )
    assert resp.status_code == 202
    core.speak.assert_called_once_with('Node01', 'hi', 'en')


def test_inject_missing_text(monkeypatch):
    """POST /inject without text -> 400."""
    app = core.app.test_client()
    resp = app.post('/inject', json={"node": "Node01"})
    assert resp.status_code == 400


def test_inject_unknown_node(monkeypatch):
    """POST /inject with unknown node -> 404."""
    app = core.app.test_client()
    resp = app.post('/inject', json={"node": "Nope", "text": "hi"})
    assert resp.status_code == 404


def test_inject_tts_engine_none(monkeypatch):
    """POST /inject when tts_engine is None -> 503."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'tts_engine', None)

    app = core.app.test_client()
    resp = app.post('/inject', json={"node": "Node01", "text": "hi"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# speak()
# ---------------------------------------------------------------------------

def test_speak_retry_and_cleanup(monkeypatch):
    """speak retries paplay; uses --device=VirtualMicSink; sets PULSE_SERVER; cleans wav."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})

    # Simulate: first paplay fails (returncode=1), second succeeds (returncode=0)
    run_calls = []

    def fake_run(args, env=None, timeout=None):
        run_calls.append({'args': args, 'env': env})
        mock_result = MagicMock()
        if len(run_calls) == 1:
            mock_result.returncode = 1
        else:
            mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.time, 'sleep', MagicMock())

    # Track the wav file created
    original_mkstemp = tempfile.mkstemp
    created_wav = {}

    def tracking_mkstemp(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        created_wav['path'] = path
        return fd, path

    monkeypatch.setattr(tempfile, 'mkstemp', tracking_mkstemp)

    core.speak('Node01', 'hello', 'en')

    # subprocess.run called exactly 2 times (1 fail + 1 success)
    assert len(run_calls) == 2

    # args contain --device=VirtualMicSink
    assert '--device=VirtualMicSink' in run_calls[0]['args']

    # env PULSE_SERVER set correctly
    assert run_calls[0]['env']['PULSE_SERVER'] == 'tcp:node01:4713'

    # temp wav file removed
    assert not os.path.exists(created_wav['path'])


# ---------------------------------------------------------------------------
# pick_tts_lang
# ---------------------------------------------------------------------------

def test_pick_tts_lang_pt():
    assert core.pick_tts_lang('pt') == 'pt'


def test_pick_tts_lang_unknown():
    assert core.pick_tts_lang('xx') == 'en'


def test_pick_tts_lang_none():
    assert core.pick_tts_lang(None) == 'en'


def test_pick_tts_lang_zh():
    assert core.pick_tts_lang('zh') == 'zh-cn'


def test_pick_tts_lang_forced_env(monkeypatch):
    monkeypatch.setenv('TTS_LANG', 'es')
    assert core.pick_tts_lang('pt') == 'es'


# ---------------------------------------------------------------------------
# query_llm with history
# ---------------------------------------------------------------------------

def test_query_llm_history_accumulates(monkeypatch):
    """Two calls on same node: second request has 4 messages (system, user, assistant, user)."""
    core._history.clear()
    monkeypatch.setattr(core, 'HISTORY_TTL', 300.0)

    captured_payloads = []

    def fake_post(url, json=None, timeout=None):
        captured_payloads.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'message': {'content': 'R1'}}
        return resp

    monkeypatch.setattr(core.requests, 'post', fake_post)

    core.query_llm('Node01', 'first')
    core.query_llm('Node01', 'second')

    # Second request should have 4 messages: system, user(first), assistant(R1), user(second)
    second_payload = captured_payloads[1]
    assert len(second_payload['messages']) == 4
    assert second_payload['messages'][0]['role'] == 'system'
    assert second_payload['messages'][1]['role'] == 'user'
    assert second_payload['messages'][1]['content'] == 'first'
    assert second_payload['messages'][2]['role'] == 'assistant'
    assert second_payload['messages'][2]['content'] == 'R1'
    assert second_payload['messages'][3]['role'] == 'user'
    assert second_payload['messages'][3]['content'] == 'second'

    core._history.clear()


def test_query_llm_history_ttl_expiry(monkeypatch):
    """With HISTORY_TTL=0 and advanced time, history is cleared between calls."""
    core._history.clear()
    monkeypatch.setattr(core, 'HISTORY_TTL', 0)

    fake_time = [1000.0]

    def fake_time_func():
        return fake_time[0]

    monkeypatch.setattr(core.time, 'time', fake_time_func)

    captured_payloads = []

    def fake_post(url, json=None, timeout=None):
        captured_payloads.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'message': {'content': 'R1'}}
        return resp

    monkeypatch.setattr(core.requests, 'post', fake_post)

    # First call
    core.query_llm('Node01', 'first')
    # Advance time so TTL expires
    fake_time[0] = 2000.0
    # Second call - history should have been cleared
    core.query_llm('Node01', 'second')

    # Second request should only have 2 messages: system + user(second)
    assert len(captured_payloads[1]['messages']) == 2
    assert captured_payloads[1]['messages'][0]['role'] == 'system'
    assert captured_payloads[1]['messages'][1]['content'] == 'second'

    core._history.clear()


def test_query_llm_error_preserves_history(monkeypatch):
    """HTTP error returns 'Cognitive failure.' and does not modify history."""
    core._history.clear()
    monkeypatch.setattr(core, 'HISTORY_TTL', 300.0)

    def fake_post(url, json=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("HTTP 500")
        return resp

    monkeypatch.setattr(core.requests, 'post', fake_post)

    result = core.query_llm('Node01', 'test')
    assert result == 'Cognitive failure.'

    # History should be empty (no messages added on error)
    assert core._history['Node01']['messages'] == []

    core._history.clear()


# ---------------------------------------------------------------------------
# ENROLL_PATTERN
# ---------------------------------------------------------------------------

def test_enroll_pattern_english():
    m = core.ENROLL_PATTERN.search('my name is John')
    assert m is not None
    assert m.group(1) == 'John'


def test_enroll_pattern_portuguese():
    m = core.ENROLL_PATTERN.search('Meu nome é Rafael')
    assert m is not None
    assert m.group(1) == 'Rafael'
