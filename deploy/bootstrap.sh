#!/usr/bin/env bash
#
# Подготовка чистого Oracle Linux 9 к запуску бота.
# Идемпотентен: можно запускать повторно, уже сделанные шаги пропускаются.
#
# Использование (на сервере, из папки проекта):
#     bash deploy/bootstrap.sh
#
# Переменные (необязательно):
#     APP_DIR=/home/opc/assistant  — куда положить проект
#     PYTHON=python3.11           — интерпретатор с версией >= 3.10
#
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/assistant}"
PYTHON="${PYTHON:-python3.11}"
REPO_URL="${REPO_URL:-https://github.com/xtrysaa/personal-assistant.git}"
SWAP_MB="${SWAP_MB:-2048}"

log() { printf '\n=== %s ===\n' "$1"; }

# --------------------------------------------------------------------------- #
log "1. Swap ${SWAP_MB} МБ"
# На E2.1.Micro доступно ~500 МБ RAM — без swap pip install падает по памяти.
if [ ! -f /swapfile ]; then
    sudo fallocate -l "${SWAP_MB}M" /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "swapfile создан"
else
    echo "swapfile уже есть"
fi
free -m | tail -2

# --------------------------------------------------------------------------- #
log "2. Пакеты"
# Системный python3 в Oracle Linux 9 — 3.9, а проекту нужен >= 3.10
# (из-за @dataclass(slots=True) в bot/config.py).
sudo dnf install -y git "$PYTHON" "${PYTHON}-pip"
echo "git:     $(git --version)"
echo "$PYTHON: $("$PYTHON" --version 2>&1)"

# --------------------------------------------------------------------------- #
log "3. Код проекта"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# --------------------------------------------------------------------------- #
log "4. Виртуальное окружение и зависимости"
if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "зависимости установлены"

# --------------------------------------------------------------------------- #
log "5. Настройки"
if [ ! -f .env ]; then
    echo "ВНИМАНИЕ: нет файла $APP_DIR/.env"
    echo "Скопируй его с локальной машины:"
    echo "  scp -i ключ.key .env opc@<IP>:$APP_DIR/.env"
    exit 1
fi
chmod 600 .env
echo ".env на месте (права 600)"

# --------------------------------------------------------------------------- #
log "6. Автозапуск через systemd"
sudo cp deploy/assistant-bot.service /etc/systemd/system/assistant-bot.service
sudo systemctl daemon-reload
sudo systemctl enable assistant-bot
sudo systemctl restart assistant-bot
sleep 6
sudo systemctl --no-pager --lines=15 status assistant-bot || true

# --------------------------------------------------------------------------- #
log "Готово"
echo "Логи в реальном времени: sudo journalctl -u assistant-bot -f"
echo "Проверить связь с Telegram и модель: см. строку 'Бот запущен' в логах."
