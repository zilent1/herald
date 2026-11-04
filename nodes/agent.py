import json
import os
import re
import subprocess
import threading
import time
import requests
import undetected_chromedriver as uc
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

# --- CONFIG ---
NODE_NAME = os.getenv("NODE_ID", "NODE.XX")
PROFILE_PATH = "/app/chrome_profile"
ESP_IP = os.getenv("ESP_IP") # The D1 Mini's IP address
REDIAL_URL = f"http://{ESP_IP}/shout" if ESP_IP else None
CALLING_URL = os.getenv("MEET_URL", "https://meet.google.com/calling/") 
COOKIE_FILE = os.getenv('COOKIE_FILE')
LOGIN_RETRY_SECONDS = int(os.getenv('LOGIN_RETRY_SECONDS', '300'))
COOKIE_INJECT = os.getenv('COOKIE_INJECT', '0') == '1'
LOGIN_POLL_SECONDS = int(os.getenv('LOGIN_POLL_SECONDS', '10'))
NOVNC_URL = os.getenv('NOVNC_URL', '')
STATUS_PORT = int(os.getenv('STATUS_PORT', '8080'))
SESSION_CHECK_SECONDS = int(os.getenv('SESSION_CHECK_SECONDS', '1800'))
REQUIRED_SINKS = ('SpeakerOutput', 'VirtualMicSink', 'SelftestSink')
KEY_COOKIES = ('SID', '__Secure-1PSID', '__Secure-3PSID')
GUEST_SELECTOR = 'input[aria-label="Your name"], input[aria-label="Seu nome"]'

# --- STATE MACHINE ---
STATE = {'state': 'starting', 'since': time.time(), 'login_failures': 0,
         'last_login_ok': None, 'calls_answered': 0, 'last_call_start': None,
         'last_call_end': None, 'last_session_check': None}
_state_lock = threading.Lock()

def set_state(name):
    with _state_lock:
        if STATE['state'] != name:
            STATE['state'] = name
            STATE['since'] = time.time()

# --- AUDIO SETUP ---
# Handled by entrypoint.sh

def load_netscape_cookies(path):
    """Parse a Netscape-format cookies file. Returns list of cookie dicts."""
    if not os.path.isfile(path):
        return []
    cookies = []
    with open(path, 'r') as f:
        for line in f:
            line = line.rstrip('\r\n')
            if not line or line.isspace():
                continue
            http_only = False
            if line.startswith('#HttpOnly_'):
                http_only = True
                line = line[len('#HttpOnly_'):]
            elif line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) < 7:
                continue
            domain, include_subdomains, cpath, secure, expiry, name, value = fields[:7]
            cookie = {
                'domain': domain,
                'path': cpath,
                'secure': secure.upper() == 'TRUE',
                'httpOnly': http_only,
                'name': name,
                'value': value,
            }
            try:
                expiry_int = int(float(expiry))
            except ValueError:
                continue
            if expiry_int > 0:
                cookie['expires'] = expiry_int
            cookies.append(cookie)
    return cookies


def inject_cookies(driver, cookies):
    """Inject cookies into the browser via CDP. Returns count of successful injections."""
    driver.execute_cdp_cmd('Network.enable', {})
    count = 0
    for cookie in cookies:
        try:
            driver.execute_cdp_cmd('Network.setCookie', cookie)
            count += 1
        except Exception as e:
            print(f"[{NODE_NAME}] [!] Cookie injection failed for '{cookie.get('name', '?')}': {e}")
    return count


def is_logged_out(url):
    """Check if the browser was redirected to a Google login page."""
    if url is None:
        return False
    if url.startswith('https://accounts.google.com'):
        return True
    if 'google.com/account/about' in url:
        return True
    return False


def page_logged_out(driver):
    """Check if the current page indicates a logged-out state (login redirect or Meet guest page)."""
    if is_logged_out(driver.current_url):
        return True
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, GUEST_SELECTOR)) > 0
    except Exception:
        return False


def ensure_session(driver):
    """Ensure a valid Google session. Default renewal is a manual login through noVNC; cookie-file injection is opt-in (COOKIE_INJECT=1)."""
    def attempt():
        set_state('logging_in')
        if COOKIE_INJECT and COOKIE_FILE:
            cookies = load_netscape_cookies(COOKIE_FILE)
            injected = inject_cookies(driver, cookies)
            print(f"[{NODE_NAME}] Injected {injected}/{len(cookies)} cookies from {COOKIE_FILE}")
        driver.get(CALLING_URL)
        time.sleep(5)
        if not page_logged_out(driver):
            now = time.time()
            with _state_lock:
                STATE['last_login_ok'] = now
                STATE['login_failures'] = 0
                STATE['last_session_check'] = now
            set_state('idle')
            return True
        with _state_lock:
            STATE['login_failures'] += 1
        set_state('logged_out')
        hint = f"Log in via noVNC {NOVNC_URL or '(noVNC disabled)'}"
        if COOKIE_INJECT:
            hint += f" or re-export cookies to {COOKIE_FILE}"
        print(f"[{NODE_NAME}] [!] Google session invalid. {hint}; watching the page every {LOGIN_POLL_SECONDS}s.")
        return False

    if attempt():
        return
    last_attempt = time.time()
    while True:
        time.sleep(LOGIN_POLL_SECONDS)
        # Don't navigate while the user is on the login form or the Meet guest page
        retry_cookies = COOKIE_INJECT and time.time() - last_attempt >= LOGIN_RETRY_SECONDS
        if page_logged_out(driver) and not retry_cookies:
            continue
        if attempt():
            print(f"[{NODE_NAME}] Google session OK.")
            return
        last_attempt = time.time()


def maybe_recheck_session(driver, now=None):
    now = now or time.time()
    if STATE['state'] != 'idle':
        return False
    last = STATE['last_session_check']
    if last is not None and now - last < SESSION_CHECK_SECONDS:
        return False
    STATE['last_session_check'] = now
    driver.refresh()
    time.sleep(5)
    if page_logged_out(driver):
        print(f"[{NODE_NAME}] [!] Session lost while idle.")
        set_state('logged_out')
        ensure_session(driver)
        return True
    return False


def cookie_summary(path):
    if not path or not os.path.isfile(path):
        return {'file': path, 'exists': False, 'age_days': None, 'days_to_expiry': None, 'key_cookies': []}
    mtime = os.path.getmtime(path)
    age_days = round((time.time() - mtime) / 86400, 1)
    cookies = load_netscape_cookies(path)
    expiries = [c['expires'] for c in cookies if c.get('name') in KEY_COOKIES and 'expires' in c]
    days_to_expiry = min(round((e - time.time()) / 86400, 1) for e in expiries) if expiries else None
    present = sorted(c['name'] for c in cookies if c.get('name') in KEY_COOKIES)
    return {'file': path, 'exists': True, 'age_days': age_days, 'days_to_expiry': days_to_expiry, 'key_cookies': present}


def audio_sinks():
    try:
        r = subprocess.run(['pactl', 'list', 'short', 'sinks'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return [line.split('\t')[1] for line in r.stdout.splitlines() if line.strip() and len(line.split('\t')) >= 2]
    except Exception:
        return []


def build_status():
    with _state_lock:
        copy = dict(STATE)
    sinks = audio_sinks()
    missing = [s for s in REQUIRED_SINKS if s not in sinks]
    session_ok = copy['state'] in ('idle', 'in_call')
    return {'node': NODE_NAME, **copy, 'state_age_s': round(time.time() - copy['since'], 1),
            'cookies': cookie_summary(COOKIE_FILE) if COOKIE_INJECT else None,
            'cookie_inject': COOKIE_INJECT, 'login_url': NOVNC_URL or None,
            'sinks': sinks, 'missing_sinks': missing,
            'audio_ok': not missing, 'session_ok': session_ok, 'ok': session_ok and not missing}


class StatusHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/status':
            self._send(200, build_status())
        elif self.path == '/health':
            s = build_status()
            self._send(200 if s['audio_ok'] else 503, {'ok': s['audio_ok']})
        else:
            self._send(404, {'error': 'not found'})

    def log_message(self, *args):
        pass


def start_status_server(port=None):
    try:
        server = ThreadingHTTPServer(('0.0.0.0', STATUS_PORT if port is None else port), StatusHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[{NODE_NAME}] Status server on http://0.0.0.0:{STATUS_PORT}/status")
        return server
    except OSError as e:
        print(f"[{NODE_NAME}] [!] Status server failed to start: {e}")
        return None


def monitor_connection(driver):
    print(f"[{NODE_NAME}] [CITADEL.WATCHDOG] Monitoring active link...")
    # This loop runs while IN a call
    while True:
        try:
            # Check if we are still on the active call page
            # Usually, Meet calls have an 'aria-label' for 'End call' or 'Leave call' (Sair / Leave call)
            # Adjust selectors based on language if necessary, but 'End call' or icon based is standard
            # We look for the "End call" button which signifies an active call.
            hangup_btns = driver.find_elements(By.XPATH, "//button[contains(@aria-label, 'Leave') or contains(@aria-label, 'Sair') or contains(@aria-label, 'End')]")
            
            # If no hangup button is found, the call has likely ended/dropped
            if len(hangup_btns) == 0:
                print(f"[{NODE_NAME}] [!] CALL DROPPED. Re-establishing connection...")
                try:
                    if REDIAL_URL:
                        requests.get(REDIAL_URL, timeout=5) # Tell the D1 Mini to shout
                    else:
                        print(f"[{NODE_NAME}] [!] Redial Trigger skipped (ESP_IP not set)")
                except Exception as e:
                    print(f"[{NODE_NAME}] [!] Redial Trigger Failed: {e}")
                
                time.sleep(10) # Wait for the Nest to react and the bot to loop back to waiting
                return # Exit watchdog to return to main loop
            
            time.sleep(5) # Low-impact polling checking for call status
            
        except Exception as e:
            print(f"[{NODE_NAME}] [Watchdog Error] {e}")
            time.sleep(5)

def chrome_major_version():
    env_val = os.getenv('CHROME_VERSION_MAIN')
    if env_val:
        return int(env_val)
    try:
        r = subprocess.run(['google-chrome', '--version'], capture_output=True, text=True, timeout=10)
        m = re.search(r'(\d+)\.\d+', r.stdout)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def main():
    start_status_server()
    print(f"[{NODE_NAME}] [RED.PHONE] Receiver Online. Loading Profile from {PROFILE_PATH}...")
    
    opts = uc.ChromeOptions()
    
    # --- PERSISTENCE & PERMISSIONS ---
    opts.add_argument(f"--user-data-dir={PROFILE_PATH}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--use-fake-ui-for-media-stream") # Auto-allow Mic/Cam
    
    # Force-Allow Permissions (Mic, Cam, Notifications) in Preferences
    prefs = {
        "profile.default_content_setting_values.notifications": 1, 
        "profile.default_content_setting_values.media_stream_mic": 1, 
        "profile.default_content_setting_values.media_stream_camera": 1,
        "profile.default_content_setting_values.geolocation": 2 # Block location
    }
    opts.add_experimental_option("prefs", prefs)
    
    # Use Xvfb display
    # (Env var DISPLAY=:99 is set in Dockerfile, but good to be explicit if needed, standard chrome uses it automatically)

    chrome_ver = chrome_major_version()
    print(f"[{NODE_NAME}] Launching Chrome (version_main={chrome_ver})...")
    driver = uc.Chrome(options=opts, version_main=chrome_ver,
                       browser_executable_path=os.getenv("CHROME_BIN", "/usr/local/bin/chrome-quiet"))
    
    try:
        ensure_session(driver)
        
        # Main Loop: Wait for Call -> Answer -> Monitor -> Loop
        while True:
            try:
                # Look for "Answer" or "Accept" or "Atender" buttons
                # Incoming call usually pops up a "Receive call" modal or bar.
                buttons = driver.find_elements(By.TAG_NAME, "button")
                answered = False
                
                for btn in buttons:
                    label = btn.get_attribute("aria-label") or ""
                    text = btn.text.lower()

                    # Ignore "Join with a code" / "Entrar com um código" buttons
                    if 'code' in text or 'código' in text:
                        continue

                    # Triggers for answering
                    if "answer" in text or "atender" in text or "accept" in label.lower() or "join" in text:
                        print(f"[{NODE_NAME}] [RED.PHONE] !!! INCOMING CALL DETECTED !!!")
                        btn.click()
                        set_state('in_call')
                        with _state_lock:
                            STATE['calls_answered'] += 1
                            STATE['last_call_start'] = time.time()
                        print(f"[{NODE_NAME}] [RED.PHONE] Call Established. Handing over to Watchdog.")
                        
                        # Wait a bit for call to fully load
                        time.sleep(5)
                        
                        # Monitor the call
                        monitor_connection(driver)

                        # Exited watchdog means call ended. Return to dashboard.
                        with _state_lock:
                            STATE['last_call_end'] = time.time()
                        set_state('idle')
                        print(f"[{NODE_NAME}] Returning to Calling Dashboard...")
                        driver.get(CALLING_URL)
                        time.sleep(5)
                        if page_logged_out(driver):
                            ensure_session(driver)
                        answered = True
                        break
                
                if not answered:
                    maybe_recheck_session(driver)
                    # Optional: Refresh occasionally to keep session alive?
                    # Or just wait. Google Meet updates dynamically.
                    time.sleep(2)
            
            except Exception as e:
                print(f"[{NODE_NAME}] [!] Scan Error: {e}")
                time.sleep(5)
                
    except Exception as e:
        print(f"[{NODE_NAME}] Fatal Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
