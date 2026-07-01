#!/usr/bin/env bash
# Идемпотентный скрипт развёртывания telemost-recorder на Ubuntu 22.04/24.04
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() {
    echo "[deploy] $*"
}

error() {
    echo "[deploy] ОШИБКА: $*" >&2
    exit 1
}

# --- Проверка ОС ---
if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    source /etc/os-release
    if [[ "${ID:-}" != "ubuntu" ]]; then
        log "Предупреждение: скрипт рассчитан на Ubuntu 22.04/24.04, обнаружено: ${ID:-unknown}"
    elif [[ "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ]]; then
        log "Предупреждение: версия Ubuntu ${VERSION_ID:-unknown}, тестировалось на 22.04/24.04"
    else
        log "ОС: Ubuntu ${VERSION_ID}"
    fi
else
    log "Предупреждение: /etc/os-release не найден, пропускаем проверку ОС"
fi

# --- Установка Docker ---
install_docker() {
    if command -v docker &>/dev/null; then
        log "Docker уже установлен: $(docker --version)"
        return 0
    fi

    log "Docker не найден, устанавливаем..."
    if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
        if ! command -v sudo &>/dev/null; then
            error "Для установки Docker нужны права root или sudo"
        fi
        curl -fsSL https://get.docker.com | sudo sh
        sudo usermod -aG docker "$USER" 2>/dev/null || true
        log "Docker установлен. Может потребоваться перелогиниться для группы docker."
    else
        curl -fsSL https://get.docker.com | sh
    fi
}

install_docker

# Проверяем доступ к Docker
if ! docker info &>/dev/null; then
    if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo &>/dev/null; then
        DOCKER="sudo docker"
    else
        error "Docker установлен, но недоступен. Запустите: sudo systemctl start docker"
    fi
else
    DOCKER="docker"
fi

# --- Установка Docker Compose plugin ---
if ! $DOCKER compose version &>/dev/null; then
    log "Docker Compose plugin не найден, устанавливаем..."
    if [[ "${EUID:-$(id -u)}" -ne 0 ]] && command -v sudo &>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y docker-compose-plugin
    else
        apt-get update
        apt-get install -y docker-compose-plugin
    fi
    log "Docker Compose установлен: $($DOCKER compose version)"
else
    log "Docker Compose уже установлен: $($DOCKER compose version)"
fi

# --- Директория для записей ---
RECORDINGS_DIR="${SCRIPT_DIR}/recordings"
mkdir -p "$RECORDINGS_DIR"
chmod 777 "$RECORDINGS_DIR"
log "Директория записей: $RECORDINGS_DIR (права 777)"

# --- Сборка образа ---
log "Сборка Docker-образа telemost-recorder..."
$DOCKER build -t telemost-recorder .

log "Образ telemost-recorder успешно собран."

# --- Logrotate для логов Docker-контейнеров (опционально) ---
LOGROTATE_FILE="/etc/logrotate.d/docker-containers"
if [[ "${EUID:-$(id -u)}" -eq 0 ]] || command -v sudo &>/dev/null; then
    LOGROTATE_CONTENT='/var/lib/docker/containers/*/*.log {
    rotate 7
    daily
    compress
    missingok
    delaycompress
    copytruncate
    maxsize 50M
}'
    if [[ ! -f "$LOGROTATE_FILE" ]]; then
        if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
            echo "$LOGROTATE_CONTENT" > "$LOGROTATE_FILE"
            log "Logrotate настроен: $LOGROTATE_FILE"
        elif command -v sudo &>/dev/null; then
            echo "$LOGROTATE_CONTENT" | sudo tee "$LOGROTATE_FILE" > /dev/null
            log "Logrotate настроен: $LOGROTATE_FILE"
        fi
    else
        log "Logrotate уже настроен: $LOGROTATE_FILE"
    fi
else
    log "Пропуск настройки logrotate (нет прав root/sudo)"
fi

# --- Готовая команда запуска ---
echo ""
echo "========================================"
echo "  Развёртывание завершено!"
echo "========================================"
echo ""
echo "Запуск записи встречи:"
echo ""
echo "  $DOCKER run --rm --ipc=host -v \"\$(pwd)/recordings:/app/recordings\" telemost-recorder \"https://telemost.yandex.ru/j/ВАШ_ID\""
echo ""
echo "С отладкой:"
echo ""
echo "  $DOCKER run --rm --ipc=host -v \"\$(pwd)/recordings:/app/recordings\" telemost-recorder \"https://telemost.yandex.ru/j/ВАШ_ID\" --debug"
echo ""
echo "Записи сохраняются в: $RECORDINGS_DIR"
echo ""
