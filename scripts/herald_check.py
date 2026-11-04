#!/usr/bin/env python3
"""Host-side monitoring CLI for the H.E.R.A.L.D. cortex HTTP API."""
import argparse
import json
import sys
import urllib.request
import urllib.error

# Exit codes
OK = 0
PIPELINE_FAIL = 1
SESSION_FAIL = 2
UNREACHABLE = 3

PRIORITY = [UNREACHABLE, SESSION_FAIL, PIPELINE_FAIL, OK]


def worst(*codes):
    """Return the worst (highest-priority) code from the given codes."""
    for c in PRIORITY:
        if c in codes:
            return c
    return OK


def http_json(method, url, body=None, timeout=10):
    """Perform an HTTP request and parse JSON response.

    Returns (status_code, data) where data is parsed JSON or None.
    Raises ConnectionError on network-level failures.
    """
    headers = {}
    data_bytes = None
    if body is not None:
        data_bytes = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(str(e))
    try:
        data = json.loads(raw) if raw else None
    except (json.JSONDecodeError, ValueError):
        data = None
    return status, data


def check_status(base_url, http=None):
    """GET /status and return (code, lines, data)."""
    http = http or http_json
    try:
        status, data = http('GET', base_url + '/status')
    except ConnectionError as e:
        return UNREACHABLE, ['cortex unreachable: ' + str(e)], None
    if data is None:
        return UNREACHABLE, ['cortex unreachable: no JSON response'], None

    lines = []
    # Engines line
    engines = data.get('engines', {})
    eng_parts = []
    for eng in ('tts', 'whisper', 'speaker_id'):
        eng_parts.append(f"{eng}={'ok' if engines.get(eng) else 'off'}")
    lines.append('engines ' + ' '.join(eng_parts))

    # Per-node lines
    nodes = data.get('nodes', {})
    code = OK
    for name, nd in nodes.items():
        stream_ok = nd.get('stream_ok', False)
        stream_age = nd.get('stream_age_s')
        age_str = f"{stream_age:.1f}" if stream_age is not None else '?'
        stream_str = ('ok' if stream_ok else 'BAD') + f'({age_str}s)'

        agent = nd.get('agent', {})
        state = None if agent.get('unreachable') else agent.get('state')
        state_str = state or 'UNREACHABLE'
        login_failures = agent.get('login_failures', 0)
        cookies = (agent.get('cookies') or {}).get('days_to_expiry')
        cookies_str = f"{cookies}d" if cookies is not None else '?'
        missing = agent.get('missing_sinks', [])
        sinks_str = 'ok' if not missing else 'missing:' + ','.join(missing)

        lines.append(
            f"{name}  stream={stream_str}  agent={state_str}  "
            f"login_failures={login_failures}  cookies={cookies_str}  sinks={sinks_str}"
        )

        # Determine worst code for this node
        if state not in ('idle', 'in_call'):
            code = worst(code, SESSION_FAIL)
        if not stream_ok or missing:
            code = worst(code, PIPELINE_FAIL)

    # Check engines
    if not engines.get('tts') or not engines.get('whisper'):
        code = worst(code, PIPELINE_FAIL)

    return code, lines, data


def run_selftests(base_url, nodes, audible=False, timeout=150, http=None):
    """POST /selftest for each node. Returns (code, lines, results)."""
    http = http or http_json
    code = OK
    lines = []
    results = {}
    for node in nodes:
        try:
            status, data = http(
                'POST', base_url + '/selftest',
                body={'node': node, 'audible': audible, 'timeout': max(10, timeout - 30)},
                timeout=timeout,
            )
        except ConnectionError as e:
            lines.append(f'{node} selftest FAIL: unreachable ({e})')
            results[node] = {'ok': False, 'error': str(e)}
            code = worst(code, UNREACHABLE)
            continue

        if status in (200, 500) and data and 'stages' in data:
            ok = data.get('ok', False)
            duration_ms = data.get('duration_ms', 0)
            lines.append(f"{node} selftest {'PASS' if ok else 'FAIL'} ({duration_ms/1000:.1f}s)")
            stages = data.get('stages', {})
            for stage_name, st in stages.items():
                t_ms = st.get('t_ms')
                t_str = str(t_ms) if t_ms is not None else '-'
                lines.append(
                    f"  {stage_name:<9}{('ok' if st.get('ok') else 'FAIL'):<5}{t_str}ms  {st.get('detail', '')}"
                )
            results[node] = data
            if not ok:
                code = worst(code, PIPELINE_FAIL)
        elif status == 409:
            lines.append(f'{node} selftest SKIPPED (busy)')
            results[node] = {'ok': True, 'skipped': True}
        else:
            err = (data or {}).get('error', f'HTTP {status}')
            lines.append(f'{node} selftest FAIL: {err}')
            results[node] = {'ok': False, 'error': err}
            code = worst(code, PIPELINE_FAIL)
    return code, lines, results


def notify(webhook, text, http=None):
    """POST text to a webhook URL. Swallows all exceptions."""
    http = http or http_json
    try:
        http('POST', webhook, body={'text': text})
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='H.E.R.A.L.D. monitoring CLI')
    parser.add_argument('--url', default='http://127.0.0.1:5000')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--audible', action='store_true')
    parser.add_argument('--node', action='append')
    parser.add_argument('--timeout', type=float, default=150)
    parser.add_argument('--webhook', default=None)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    code, lines, data = check_status(args.url)
    results = None
    if args.selftest and data is not None:
        nodes = args.node or list(data.get('nodes', {}).keys())
        code2, lines2, results = run_selftests(
            args.url, nodes, audible=args.audible, timeout=args.timeout
        )
        lines += lines2
        code = worst(code, code2)

    if args.json:
        print(json.dumps({'code': code, 'status': data, 'selftest': results}, indent=2))
    else:
        for line in lines:
            print(line)

    if code != OK and args.webhook:
        notify(args.webhook, '\n'.join(lines))

    return code


if __name__ == '__main__':
    sys.exit(main())
