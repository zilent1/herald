FROM python:3.10-slim-bookworm

# Install Audio Subsystem
RUN apt-get update && apt-get install -y pulseaudio pulseaudio-utils libasound2 alsa-utils wget gnupg unzip xvfb \
    x11vnc \
    novnc \
    websockify

# Install Chrome
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable

WORKDIR /app
RUN pip install undetected-chromedriver selenium pulsectl

# Create a non-root user for PulseAudio
# PulseAudio refuses to run in system mode without specific flags, and user mode is safer/easier
RUN useradd -m -s /bin/bash herald

# Configs
COPY daemon.conf /etc/pulse/daemon.conf
# COPY client.conf /etc/pulse/client.conf # Removed to prevent startup issues
COPY agent.py .
# COPY default.pa /etc/pulse/default.pa # Removed to prevent recursion loop
COPY entrypoint.sh .

# Allow cookies and app dir access, make entrypoint executable
# Create persistent profile directory
USER root
RUN mkdir -p /app/chrome_profile && \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && \
    chown -R herald:herald /app && \
    chmod +x /app/entrypoint.sh

# Configure PulseAudio (Append to default.pa instead of overwriting)
RUN echo >> /etc/pulse/default.pa && \
    echo "load-module module-native-protocol-tcp auth-anonymous=1 port=4713" >> /etc/pulse/default.pa && \
    echo "load-module module-always-sink" >> /etc/pulse/default.pa

USER herald
ENV DISPLAY=:99

# TCP Audio Port
EXPOSE 4713
# noVNC WebSocket Port
EXPOSE 6080

# Use the entrypoint script
CMD ["/app/entrypoint.sh"]
