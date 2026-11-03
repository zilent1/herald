import os
import time
import requests
import undetected_chromedriver as uc
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


def ensure_session(driver):
    """Ensure the browser has a valid Google session. Retries indefinitely if cookies are expired."""
    while True:
        if COOKIE_FILE:
            cookies = load_netscape_cookies(COOKIE_FILE)
            injected = inject_cookies(driver, cookies)
            print(f"[{NODE_NAME}] Injected {injected}/{len(cookies)} cookies from {COOKIE_FILE}")
        driver.get(CALLING_URL)
        time.sleep(5)
        if not is_logged_out(driver.current_url):
            return
        print(f"[{NODE_NAME}] [!] Google session invalid (redirected to login). Re-export cookies to {COOKIE_FILE}; retrying in {LOGIN_RETRY_SECONDS}s.")
        time.sleep(LOGIN_RETRY_SECONDS)


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

def main():
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

    print(f"[{NODE_NAME}] Launching Chrome...")
    driver = uc.Chrome(options=opts, version_main=None)
    
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
                        print(f"[{NODE_NAME}] [RED.PHONE] Call Established. Handing over to Watchdog.")
                        
                        # Wait a bit for call to fully load
                        time.sleep(5)
                        
                        # Monitor the call
                        monitor_connection(driver)
                        
                        # Exited watchdog means call ended. Return to dashboard.
                        print(f"[{NODE_NAME}] Returning to Calling Dashboard...")
                        driver.get(CALLING_URL)
                        time.sleep(5)
                        if is_logged_out(driver.current_url):
                            ensure_session(driver)
                        answered = True
                        break
                
                if not answered:
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
