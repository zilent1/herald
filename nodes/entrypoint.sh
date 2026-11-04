#!/bin/bash
set -e

echo "[ENTRYPOINT] Cleaning up stale locks..."
rm -rf /tmp/pulse-* /home/herald/.config/pulse/*.pid /tmp/.X99-lock
# Chrome profile lives in a volume: a lock left by a previous container (other hostname) blocks startup
rm -f /app/chrome_profile/SingletonLock /app/chrome_profile/SingletonCookie /app/chrome_profile/SingletonSocket

echo "[ENTRYPOINT] Starting Xvfb..."
Xvfb :99 -screen 0 1280x1024x24 &
export DISPLAY=:99
sleep 1

if [ "${NOVNC_ENABLED}" = "1" ]; then
    echo "[HERALD] Starting noVNC on port 6080..."
    x11vnc -display :99 -forever -nopw -rfbport 5900 -shared -bg -o /tmp/x11vnc.log
    websockify --daemon --web /usr/share/novnc 6080 localhost:5900
fi

echo "[ENTRYPOINT] Starting PulseAudio (Debug Mode)..."
# Run in foreground (backgrounded) with moderate verbosity
pulseaudio --daemonize=no --exit-idle-time=-1 --log-target=stderr -v &
PA_PID=$!

echo "[ENTRYPOINT] Waiting for PulseAudio to stabilize..."
sleep 5

# Check if process is still alive
if ! kill -0 $PA_PID > /dev/null 2>&1; then
    echo "[!] PulseAudio crashed immediately!"
    # The logs should be above
    exit 1
fi

echo "[ENTRYPOINT] Configuring sinks..."
# We can just run the commands directly here instead of in python, but python is fine too.
# Doing it here ensures it's done before app starts.
pactl load-module module-null-sink sink_name=SpeakerOutput sink_properties=device.description=SpeakerOutput || true
pactl load-module module-null-sink sink_name=VirtualMicSink sink_properties=device.description=VirtualMicSink || true
pactl load-module module-null-sink sink_name=SelftestSink sink_properties=device.description=SelftestSink || true
pactl load-module module-remap-source master=VirtualMicSink.monitor source_name=VirtualMic source_properties=device.description=VirtualMic || true
pactl set-default-sink SpeakerOutput || true
pactl set-default-source VirtualMic || true

echo "[ENTRYPOINT] Starting Agent..."
python -u agent.py
