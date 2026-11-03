FROM python:3.10-slim-bookworm

WORKDIR /app

# Install system dependencies for TTS and Audio
RUN apt-get update && apt-get install -y \
    libsndfile1 \
    espeak-ng \
    ffmpeg \
    pulseaudio-utils \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# openWakeWord >= 0.5 ships without model files: fetch the wake word + feature models at build time
RUN python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"

COPY core.py .
COPY speakers.py .
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
CMD ["python", "core.py"]
