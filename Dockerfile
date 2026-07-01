FROM mcr.microsoft.com/playwright/python:v1.44.0-noble

# FFmpeg для извлечения аудио; системные библиотеки для Chromium в headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN playwright install chromium

# Non-root пользователь для безопасности в production
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/recordings \
    && chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/recordings"]

ENTRYPOINT ["python", "main.py"]
