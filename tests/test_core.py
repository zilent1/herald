import sys
import os
import json
import tempfile
import queue
import time
from unittest.mock import MagicMock, call

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


# ---------------------------------------------------------------------------
# Event bus: emit / subscribe / unsubscribe
# ---------------------------------------------------------------------------

def test_emit_delivers_to_subscribed_queue():
    """emit puts event on the subscribed queue."""
    q = core.subscribe('TestNode')
    try:
        core.emit('TestNode', 'wake')
        event = q.get_nowait()
        assert event['stage'] == 'wake'
        assert 'ts' in event
    finally:
        core.unsubscribe('TestNode', q)


def test_emit_not_delivered_after_unsubscribe():
    """After unsubscribe, emit no longer delivers to that queue."""
    q = core.subscribe('TestNode')
    core.unsubscribe('TestNode', q)
    core.emit('TestNode', 'wake')
    assert q.empty()


def test_emit_wake_updates_last_wake_ts():
    """emit('wake') updates node_state last_wake_ts."""
    core.emit('TestNode', 'wake')
    st = core.node_state('TestNode')
    assert st['last_wake_ts'] > 0.0


def test_emit_transcribed_updates_last_command_ts():
    """emit('transcribed') updates node_state last_command_ts."""
    core.emit('TestNode', 'transcribed', text='hello', lang='en')
    st = core.node_state('TestNode')
    assert st['last_command_ts'] > 0.0


# ---------------------------------------------------------------------------
# speak() return value and sink parameter
# ---------------------------------------------------------------------------

def test_speak_returns_true_on_success(monkeypatch):
    """speak returns True when paplay succeeds on first attempt."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})

    def fake_run(args, env=None, timeout=None):
        mock_result = MagicMock()
        mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.time, 'sleep', MagicMock())

    result = core.speak('Node01', 'hello', 'en')
    assert result is True


def test_speak_returns_false_when_all_attempts_fail(monkeypatch):
    """speak returns False when all 3 paplay attempts fail."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})

    def fake_run(args, env=None, timeout=None):
        mock_result = MagicMock()
        mock_result.returncode = 1
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.time, 'sleep', MagicMock())

    result = core.speak('Node01', 'hello', 'en')
    assert result is False


def test_speak_uses_selftest_sink(monkeypatch):
    """speak with sink='SelftestSink' uses --device=SelftestSink."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})

    run_calls = []

    def fake_run(args, env=None, timeout=None):
        run_calls.append(args)
        mock_result = MagicMock()
        mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.time, 'sleep', MagicMock())

    core.speak('Node01', 'hello', 'en', sink='SelftestSink')
    assert '--device=SelftestSink' in run_calls[0]


def test_speak_emits_tts_sent(monkeypatch):
    """speak emits tts_sent event with ok and sink."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})

    def fake_run(args, env=None, timeout=None):
        mock_result = MagicMock()
        mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.time, 'sleep', MagicMock())

    q = core.subscribe('Node01')
    try:
        core.speak('Node01', 'hello', 'en')
        event = q.get_nowait()
        assert event['stage'] == 'tts_sent'
        assert event['ok'] is True
        assert event['sink'] == 'VirtualMicSink'
    finally:
        core.unsubscribe('Node01', q)


# ---------------------------------------------------------------------------
# query_llm(use_history=False)
# ---------------------------------------------------------------------------

def test_query_llm_no_history(monkeypatch):
    """query_llm with use_history=False sends exactly 2 messages and leaves _history empty."""
    core._history.clear()
    captured_payloads = []

    def fake_post(url, json=None, timeout=None):
        captured_payloads.append(json)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {'message': {'content': 'answer'}}
        return resp

    monkeypatch.setattr(core.requests, 'post', fake_post)

    result = core.query_llm('Node01', 'test', use_history=False)
    assert result == 'answer'

    # Exactly 2 messages: system + user
    assert len(captured_payloads[0]['messages']) == 2
    assert captured_payloads[0]['messages'][0]['role'] == 'system'
    assert captured_payloads[0]['messages'][1]['role'] == 'user'
    assert captured_payloads[0]['messages'][1]['content'] == 'test'

    # _history should be empty (no entry created for this node)
    assert 'Node01' not in core._history

    core._history.clear()


# ---------------------------------------------------------------------------
# process_command with selftest
# ---------------------------------------------------------------------------

def test_process_command_selftest_run(monkeypatch):
    """process_command with a pending selftest run: skips speaker, uses use_history=False, sink=SelftestSink."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'transcribe_audio', MagicMock(return_value=('what is two plus two', 'en')))
    monkeypatch.setattr(core, 'query_llm', MagicMock(return_value='four'))
    monkeypatch.setattr(core, 'speak', MagicMock())
    monkeypatch.setattr(core, 'embed_speaker', MagicMock())
    core._selftest_pending['Node01'] = {}

    try:
        core.process_command('Node01', b'\x00' * 32000)
    finally:
        core._selftest_pending.pop('Node01', None)

    # query_llm called with use_history=False
    core.query_llm.assert_called_once_with('Node01', 'what is two plus two', use_history=False)

    # speak called with sink='SelftestSink'
    core.speak.assert_called_once_with('Node01', 'four', language='en', sink='SelftestSink')

    # embed_speaker never called
    core.embed_speaker.assert_not_called()


def test_process_command_transcribed_empty(monkeypatch):
    """process_command emits 'transcribed' with empty text when transcription is empty."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'transcribe_audio', MagicMock(return_value=('', 'en')))
    monkeypatch.setattr(core, 'embed_speaker', MagicMock())

    q = core.subscribe('Node01')
    try:
        core.process_command('Node01', b'\x00' * 32000)
        event = q.get_nowait()
        assert event['stage'] == 'transcribed'
        assert event['text'] == ''
    finally:
        core.unsubscribe('Node01', q)


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------

def _make_run(events=None, started=None, stream_age=1.0, rms=500.0, played=True):
    if started is None:
        started = time.time()
    return {
        'started': started,
        'stream_age': stream_age,
        'events': events or [],
        'rms': rms,
        'played': played,
    }


def test_evaluate_all_ok():
    """evaluate with a complete good run -> ok True and all 6 stages ok in order."""
    started = time.time()
    events = [
        {'stage': 'wake', 'ts': started + 0.1},
        {'stage': 'transcribed', 'ts': started + 1.0, 'text': 'What is two plus two?', 'lang': 'en'},
        {'stage': 'llm', 'ts': started + 2.0, 'reply': 'Four.'},
        {'stage': 'tts_sent', 'ts': started + 3.0, 'ok': True, 'sink': 'SelftestSink'},
    ]
    run = _make_run(events=events, started=started, stream_age=0.5, rms=500.0, played=True)
    ok, stages = core.evaluate(run)
    assert ok is True
    assert list(stages.keys()) == ['stream', 'stimulus', 'wake', 'stt', 'llm', 'tts']
    for s in stages.values():
        assert s['ok'] is True


def test_evaluate_no_events():
    """evaluate with no events -> wake/stt/llm/tts not ok with detail 'no event'."""
    run = _make_run(events=[], stream_age=1.0, played=True)
    ok, stages = core.evaluate(run)
    assert ok is False
    assert stages['wake']['ok'] is False
    assert stages['wake']['detail'] == 'no event'
    assert stages['stt']['ok'] is False
    assert stages['stt']['detail'] == 'no event'
    assert stages['llm']['ok'] is False
    assert stages['llm']['detail'] == 'no event'
    assert stages['tts']['ok'] is False
    assert stages['tts']['detail'] == 'no event'


def test_evaluate_stt_no_keyword():
    """evaluate stt fails when text has no keyword."""
    started = time.time()
    events = [
        {'stage': 'wake', 'ts': started + 0.1},
        {'stage': 'transcribed', 'ts': started + 1.0, 'text': 'hello world', 'lang': 'en'},
    ]
    run = _make_run(events=events, started=started, stream_age=1.0, played=True)
    ok, stages = core.evaluate(run)
    assert ok is False
    assert stages['stt']['ok'] is False


def test_evaluate_llm_cognitive_failure():
    """evaluate llm fails on 'Cognitive failure.'."""
    started = time.time()
    events = [
        {'stage': 'wake', 'ts': started + 0.1},
        {'stage': 'transcribed', 'ts': started + 1.0, 'text': 'two plus two', 'lang': 'en'},
        {'stage': 'llm', 'ts': started + 2.0, 'reply': 'Cognitive failure.'},
    ]
    run = _make_run(events=events, started=started, stream_age=1.0, played=True)
    ok, stages = core.evaluate(run)
    assert ok is False
    assert stages['llm']['ok'] is False


def test_evaluate_tts_rms_below_min():
    """evaluate tts fails when rms below SELFTEST_RMS_MIN."""
    started = time.time()
    events = [
        {'stage': 'wake', 'ts': started + 0.1},
        {'stage': 'transcribed', 'ts': started + 1.0, 'text': 'two plus two', 'lang': 'en'},
        {'stage': 'llm', 'ts': started + 2.0, 'reply': 'Four.'},
        {'stage': 'tts_sent', 'ts': started + 3.0, 'ok': True, 'sink': 'SelftestSink'},
    ]
    run = _make_run(events=events, started=started, stream_age=1.0, rms=100.0, played=True)
    ok, stages = core.evaluate(run)
    assert ok is False
    assert stages['tts']['ok'] is False


def test_evaluate_stream_none():
    """evaluate stream fails when stream_age is None."""
    run = _make_run(events=[], stream_age=None, played=True)
    ok, stages = core.evaluate(run)
    assert ok is False
    assert stages['stream']['ok'] is False
    assert stages['stream']['detail'] == 'no audio'


# ---------------------------------------------------------------------------
# /selftest endpoint
# ---------------------------------------------------------------------------

def test_selftest_missing_node():
    """POST /selftest without node -> 400."""
    app = core.app.test_client()
    resp = app.post('/selftest', json={})
    assert resp.status_code == 400


def test_selftest_unknown_node():
    """POST /selftest with unknown node -> 404."""
    app = core.app.test_client()
    resp = app.post('/selftest', json={"node": "Nope"})
    assert resp.status_code == 404


def test_selftest_engine_unavailable(monkeypatch):
    """POST /selftest when whisper_model is None -> 503."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'whisper_model', None)
    monkeypatch.setattr(core, 'tts_engine', MagicMock())

    app = core.app.test_client()
    resp = app.post('/selftest', json={"node": "Node01"})
    assert resp.status_code == 503


def test_selftest_busy(monkeypatch):
    """POST /selftest when node is busy -> 409."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'tts_engine', MagicMock())
    monkeypatch.setattr(core, 'whisper_model', MagicMock())
    core.node_state('Node01')['busy'] = True

    try:
        app = core.app.test_client()
        resp = app.post('/selftest', json={"node": "Node01"})
        assert resp.status_code == 409
    finally:
        core.node_state('Node01')['busy'] = False


def test_selftest_success(monkeypatch):
    """POST /selftest with run_selftest returning ok -> 200."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    monkeypatch.setattr(core, 'tts_engine', MagicMock())
    monkeypatch.setattr(core, 'whisper_model', MagicMock())
    monkeypatch.setattr(core, 'run_selftest', MagicMock(return_value={'ok': True, 'node': 'Node01', 'stages': {}}))

    app = core.app.test_client()
    resp = app.post('/selftest', json={"node": "Node01"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# run_selftest
# ---------------------------------------------------------------------------

def test_run_selftest_full(monkeypatch):
    """run_selftest with monkeypatched subprocess.run/get_stimulus/MonitorRecorder -> ok True, cleanup verified."""
    monkeypatch.setitem(core.NODES, 'Node01', {'host': 'node01', 'port': '4713'})
    # Set last_audio_ts so stream_age is fresh
    st = core.node_state('Node01')
    st['last_audio_ts'] = time.time()

    monkeypatch.setattr(core, 'get_stimulus', MagicMock(return_value='/fake/stimulus.wav'))

    class FakeRecorder:
        def __init__(self, node_name, source):
            pass
        def start(self):
            pass
        def stop(self):
            return 1000.0

    monkeypatch.setattr(core, 'MonitorRecorder', FakeRecorder)

    def fake_run(args, env=None, timeout=None):
        # Simulate the pipeline events
        core.emit('Node01', 'wake')
        core.emit('Node01', 'transcribed', text='two plus two', lang='en')
        core.emit('Node01', 'llm', reply='Four.')
        core.emit('Node01', 'tts_sent', ok=True, sink='SelftestSink')
        mock_result = MagicMock()
        mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr(core.subprocess, 'run', fake_run)

    result = core.run_selftest('Node01')

    assert result['ok'] is True
    assert result['node'] == 'Node01'
    # Cleanup: node not in _selftest_pending
    assert 'Node01' not in core._selftest_pending
    # _listeners for that node is empty
    assert core._listeners.get('Node01', []) == []


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------

def test_status_ok(monkeypatch):
    """GET /status with healthy node -> ok True, stream_ok True."""
    monkeypatch.setattr(core, 'NODES', {'Node01': {'host': 'node01', 'port': '4713'}})
    monkeypatch.setattr(core, 'tts_engine', MagicMock())
    monkeypatch.setattr(core, 'whisper_model', MagicMock())
    monkeypatch.setattr(core, 'speaker_embedder', MagicMock())
    monkeypatch.setattr(core, 'fetch_node_status', lambda name: {'ok': True, 'state': 'idle'})
    st = core.node_state('Node01')
    st['stream_connected'] = True
    st['last_audio_ts'] = time.time()
    try:
        app = core.app.test_client()
        resp = app.get('/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert data['nodes']['Node01']['stream_ok'] is True
        assert data['nodes']['Node01']['ok'] is True
    finally:
        core.NODE_STATE.pop('Node01', None)

def test_status_agent_down(monkeypatch):
    """GET /status with agent logged_out -> ok False, node ok False."""
    monkeypatch.setattr(core, 'NODES', {'Node01': {'host': 'node01', 'port': '4713'}})
    monkeypatch.setattr(core, 'tts_engine', MagicMock())
    monkeypatch.setattr(core, 'whisper_model', MagicMock())
    monkeypatch.setattr(core, 'speaker_embedder', MagicMock())
    monkeypatch.setattr(core, 'fetch_node_status', lambda name: {'ok': False, 'state': 'logged_out'})
    st = core.node_state('Node01')
    st['stream_connected'] = True
    st['last_audio_ts'] = time.time()
    try:
        app = core.app.test_client()
        resp = app.get('/status')
        data = resp.get_json()
        assert data['ok'] is False
        assert data['nodes']['Node01']['ok'] is False
    finally:
        core.NODE_STATE.pop('Node01', None)

def test_status_stream_stale(monkeypatch):
    """GET /status with last_audio_ts 60s ago -> stream_ok False."""
    monkeypatch.setattr(core, 'NODES', {'Node01': {'host': 'node01', 'port': '4713'}})
    monkeypatch.setattr(core, 'tts_engine', MagicMock())
    monkeypatch.setattr(core, 'whisper_model', MagicMock())
    monkeypatch.setattr(core, 'speaker_embedder', MagicMock())
    monkeypatch.setattr(core, 'fetch_node_status', lambda name: {'ok': True, 'state': 'idle'})
    st = core.node_state('Node01')
    st['stream_connected'] = True
    st['last_audio_ts'] = time.time() - 60
    try:
        app = core.app.test_client()
        resp = app.get('/status')
        data = resp.get_json()
        assert data['nodes']['Node01']['stream_ok'] is False
    finally:
        core.NODE_STATE.pop('Node01', None)

def test_fetch_node_status_unreachable(monkeypatch):
    """fetch_node_status with requests.get raising -> returns unreachable True."""
    monkeypatch.setattr(core, 'NODES', {'Node01': {'host': 'node01', 'port': '4713'}})

    def fake_get(*a, **kw):
        raise Exception('boom')
    monkeypatch.setattr(core.requests, 'get', fake_get)
    result = core.fetch_node_status('Node01')
    assert result['unreachable'] is True
    assert result['ok'] is False
