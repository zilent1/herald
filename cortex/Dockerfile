FROM python:3.10-slim-bullseye

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

COPY core.py .
COPY speakers.py .
RUN mkdir -p /app/data

CMD ["python", "core.py"]
