# Telemost Recorder

Production-ready CLI-инструмент в Docker-контейнере для записи встреч **Яндекс.Телемост** в строго **анонимном режиме**.

Контейнер стартует → открывает ссылку → вводит имя → подключается к встрече → записывает видео+звук через Playwright → извлекает аудио через FFmpeg → сохраняет файл → завершается.

**Не поддерживается:** авторизация, API, Redis, веб-сервер, очереди.

---

## Быстрый старт

```bash
# 1. Клонировать / скопировать проект на Ubuntu-сервер
cd telemost-recorder

# 2. Развернуть (установит Docker, соберёт образ)
chmod +x deploy.sh
./deploy.sh

# 3. Записать встречу
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings telemost-recorder "https://telemost.yandex.ru/j/XXXXXXXX"
```

---

## Примеры запуска

```bash
# Базовый запуск (аудио в формате Opus)
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890"

# MP3 вместо Opus
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890" --format mp3

# Ограничение длительности — 1 час
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890" --max-duration 3600

# Режим отладки (headed + скриншоты на каждом шаге)
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890" --debug

# Кастомное имя в списке участников
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890" \
  --bot-name "Запись встречи"

# Уменьшенное разрешение видео (меньше RAM)
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "https://telemost.yandex.ru/j/1234567890" --video-resolution 480x270
```

---

## Аргументы CLI

| Аргумент | Обязательный | По умолчанию | Описание |
|----------|:---:|---|---|
| `meeting_url` | ✅ | — | HTTPS-ссылка на встречу `telemost.yandex.ru` |
| `--output-dir` | | `/app/recordings` | Директория для сохранения аудиофайлов |
| `--bot-name` | | `🤖 AI Ассистент` | Имя, отображаемое в списке участников |
| `--max-duration` | | `14400` (4 ч) | Максимальная длительность записи в секундах |
| `--format` | | `opus` | Формат аудио: `opus` или `mp3` |
| `--video-resolution` | | `640x360` | Разрешение видеозаписи (влияет на RAM) |
| `--debug` | | выкл. | Headed-режим + скриншоты на каждом шаге |

### Коды выхода

| Код | Значение |
|:---:|---|
| `0` | Успешная запись |
| `1` | Общая ошибка (URL, DOM, FFmpeg, браузер) |
| `2` | Встреча требует авторизации |
| `130` | Прервано SIGINT (Ctrl+C), частичная запись сохранена |
| `143` | Прервано SIGTERM, частичная запись сохранена |

---

## Структура проекта

```
telemost-recorder/
├── main.py              # CLI entry point (argparse)
├── recorder.py          # TelemostRecorder — Playwright lifecycle
├── dom_scanner.py       # Семантический поиск элементов предкомнаты
├── audio_extractor.py   # Извлечение аудио через FFmpeg
├── Dockerfile
├── requirements.txt
├── deploy.sh            # Развёртывание на Ubuntu
└── README.md
```

---

## Логи и файлы

**Stdout** — emoji-маркеры прогресса:
```
🔗 Открытие встречи...
👤 Ввод имени...
✅ Подключение к встрече...
⏺ Запись начата
⏹ Встреча завершена
🎵 Извлечение аудио...
💾 Сохранено: /app/recordings/20260702_120000_j_abc123.opus
```

**Логи Docker:**
```bash
docker logs <container_id>
```

**Записи:** `./recordings/` на хосте (маунтится в `/app/recordings`).

**Очистка записей:**
```bash
rm -f recordings/*.opus recordings/*.mp3 recordings/*.webm
rm -rf recordings/debug_*
```

**Debug-артефакты** (при `--debug`): `recordings/debug_YYYYMMDD_HHMMSS/step_NN_*.png`

---

## Troubleshooting

### Бот не находит кнопку / поле имени

Запустите с `--debug` и проверьте скриншоты в `recordings/debug_*`:

```bash
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "URL" --debug
```

В stdout будут перечислены все найденные кандидаты (тег, текст, aria-label, bbox). Если UI Телемоста изменился — обновите regex-паттерны в `dom_scanner.py`.

### Встреча требует вход в аккаунт

```
❌ Встреча требует авторизации. Анонимный вход недоступен.
```

Организатор встречи отключил гостевой вход. Бот **не поддерживает авторизацию** — это by design. Попросите организатора включить анонимное подключение.

Скриншот и HTML сохраняются в `recordings/debug_*/auth_required_*`.

### OOM (нехватка памяти)

Chromium в Docker потребляет 300–800 МБ RAM. Решения:

```bash
# Ограничить память контейнера
docker run --rm --ipc=host --memory=1g -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "URL" --video-resolution 480x270

# Или уменьшить разрешение видеозаписи
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings \
  telemost-recorder "URL" --video-resolution 320x240
```

### FFmpeg: нет аудиодорожки в видео

Playwright WebM на Linux headless может не содержать звук встречи — это ограничение Chromium, а не бага бота. Проверьте наличие аудио:

```bash
ffprobe recordings/partial_*.webm
```

Если аудиопотока нет — потребуется исследование альтернативных методов захвата звука.

### Graceful shutdown

`Ctrl+C` или `docker stop` инициируют сохранение частичной записи:

```bash
docker run --rm --ipc=host -v $(pwd)/recordings:/app/recordings telemost-recorder "URL"
# В другом терминале: docker stop <container_id>
```

---

## Юридическое предупреждение

**Скрытая запись разговоров без уведомления участников является нарушением законодательства РФ:**

- **Ст. 138 УК РФ** — незаконное ограничение тайны переписки, телефонных переговоров и иных сообщений
- **152-ФЗ** — обработка персональных данных без согласия субъектов

**Участники встречи ДОЛЖНЫ быть уведомлены о записи.** Бот отображается в списке участников под именем `🤖 AI Ассистент` (или заданным через `--bot-name`) — это не заменяет юридически значимое уведомление.

Используйте инструмент только с согласия всех участников встречи и в рамках применимого законодательства.

---

## Технический стек

- Python 3.11+
- Playwright 1.60.0 (async API, `headless=new`)
- FFmpeg (системный бинарник)
- Docker (`mcr.microsoft.com/playwright/python:v1.60.0-noble`)
- Целевая ОС: Ubuntu Server 22.04/24.04 (без GUI, без X11, без PulseAudio)
