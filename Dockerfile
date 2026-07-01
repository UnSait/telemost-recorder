FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# FFmpeg для извлечения аудио; зависимости Chromium уже есть в базовом образе Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN playwright install chromium

# В базовом образе Playwright уже есть non-root пользователь pwuser (UID 1000)
RUN mkdir -p /app/recordings \
    && chown -R pwuser:pwuser /app

USER pwuser

VOLUME ["/app/recordings"]

ENTRYPOINT ["python", "main.py"]
