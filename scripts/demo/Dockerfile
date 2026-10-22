# Recorder for the demo video: Playwright's Chromium + ffmpeg, nothing on the host.
#   docker build -t networth-recorder scripts/demo
FROM mcr.microsoft.com/playwright:v1.48.2-noble
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-color-emoji fonts-inter \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /rec
RUN npm init -y >/dev/null && npm install playwright@1.48.2 >/dev/null
