#!/usr/bin/env bash
#
# Развёртывание БЕЗ dnf и БЕЗ git — только curl, tar и sudo.
#
# Зачем этот вариант: на VM.Standard.E2.1.Micro доступно ~500 МБ RAM, и
# `dnf install python3.11` на такой машине уходит в многоминутный свопинг
# и может подвесить sshd. Здесь системные пакеты не трогаются вообще:
#   * Python скачивается готовой сборкой (python-build-standalone);
#   * код берётся архивом с GitHub, а не через git clone.
# Подходит и для любого другого Linux (Debian, Ubuntu, Alpine — везде, где есть curl и tar).
#
# Использование на сервере:
#     bash deploy/bootstrap-standalone.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/assistant}"
PY_SERIES="${PY_SERIES:-3.11}"
PY_ROOT="${PY_ROOT:-$HOME/.local/python}"
ARCH="$(uname -m)"
LIBC="unknown-linux-gnu"
[ "$ARCH" = "aarch64" ] && LIBC="unknown-linux-gnu"   # имя сборки совпадает
REPO_TARBALL="${REPO_TARBALL:-https://github.com/xtrysaa/personal-assistant/archive/refs/heads/main.tar.gz}"
SWAP_MB="${SWAP_MB:-2048}"

log() { printf '\n=== %s ===\n' "$1"; }

# --------------------------------------------------------------------------- #
log "1. Базовые утилиты"
for tool in curl tar; do
    command -v "$tool" >/dev/null || { echo "ОШИБКА: нет $tool"; exit 1; }
done
echo "curl и tar на месте ($ARCH)"

# --------------------------------------------------------------------------- #
log "2. Swap ${SWAP_MB} МБ (страховка для pip)"
if swapon --show 2>/dev/null | grep -q '/swapfile'; then
    echo "swap уже активен"
else
    if [ ! -f /swapfile ]; then
        sudo fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null \
            || sudo dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none
    fi
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "swap включён"
fi
free -m | tail -2

# --------------------------------------------------------------------------- #
log "3. Python ${PY_SERIES} готовой сборкой"
if [ -x "$PY_ROOT/bin/python3" ] && "$PY_ROOT/bin/python3" --version >/dev/null 2>&1; then
    echo "уже установлен: $("$PY_ROOT/bin/python3" --version 2>&1)"
else
    # Ищем актуальный ассет в последнем релизе python-build-standalone —
    # так ссылка не протухнет, когда выйдет новый релиз.
    echo "ищу сборку в последнем релизе..."
    TARBALL_URL="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
        | tr ',' '\n' \
        | grep -o 'https://[^"]*install_only\.tar\.gz' \
        | grep "cpython-${PY_SERIES}" \
        | grep "${ARCH}-${LIBC}" \
        | head -1)"

    if [ -z "$TARBALL_URL" ]; then
        echo "ОШИБКА: не нашёл сборку cpython-${PY_SERIES} для ${ARCH}. Проверь PY_SERIES."
        exit 1
    fi
    echo "качаю: $TARBALL_URL"

    mkdir -p "$(dirname "$PY_ROOT")"
    curl -fL "$TARBALL_URL" -o /tmp/python-standalone.tar.gz
    # В архиве верхний каталог python/ — распаковываем в родителя PY_ROOT
    tar -xzf /tmp/python-standalone.tar.gz -C "$(dirname "$PY_ROOT")"
    rm -f /tmp/python-standalone.tar.gz
    echo "установлен: $("$PY_ROOT/bin/python3" --version 2>&1)"
fi

# --------------------------------------------------------------------------- #
log "4. Код проекта (архивом, без git)"
if [ -d "$APP_DIR" ] && [ -f "$APP_DIR/run.py" ]; then
    echo "обновляю существующую копию в $APP_DIR"
fi
mkdir -p "$APP_DIR"
curl -fL "$REPO_TARBALL" -o /tmp/app.tar.gz
tar -xzf /tmp/app.tar.gz -C "$APP_DIR" --strip-components=1
rm -f /tmp/app.tar.gz
cd "$APP_DIR"
echo "файлов: $(find . -type f -not -path './.venv/*' | wc -l)"

# --------------------------------------------------------------------------- #
log "5. Виртуальное окружение и зависимости"
[ -d .venv ] || "$PY_ROOT/bin/python3" -m venv .venv
# Страховка: в некоторых сборках ensurepip не отрабатывает автоматически
[ -x .venv/bin/pip ] || .venv/bin/python -m ensurepip --upgrade
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt
echo "готово: $(.venv/bin/pip list --format=freeze | wc -l) пакетов"

# --------------------------------------------------------------------------- #
log "6. Настройки"
if [ ! -f .env ]; then
    echo "ВНИМАНИЕ: нет файла $APP_DIR/.env"
    echo "Скопируй с локальной машины:"
    echo "  scp -i ключ.key .env opc@<IP>:$APP_DIR/.env"
    exit 1
fi
chmod 600 .env
echo ".env на месте (права 600)"

# --------------------------------------------------------------------------- #
log "7. Проверка логики (без сети и Telegram)"
.venv/bin/python scripts/smoke_test.py | tail -3

# --------------------------------------------------------------------------- #
log "8. Автозапуск через systemd"
if [ -f deploy/assistant-bot.service ]; then
    # В юните зашиты пути /home/opc/assistant — правим, если APP_DIR другой
    sed "s#/home/opc/assistant#$APP_DIR#g; s#^User=.*#User=$(id -un)#" \
        deploy/assistant-bot.service | sudo tee /etc/systemd/system/assistant-bot.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable assistant-bot >/dev/null 2>&1
    sudo systemctl restart assistant-bot
    sleep 8
    sudo systemctl --no-pager --lines=12 status assistant-bot || true
else
    echo "юнит не найден — запусти вручную: cd $APP_DIR && .venv/bin/python run.py"
fi

log "Готово"
echo "Логи в реальном времени: sudo journalctl -u assistant-bot -f"
echo "Затем напиши боту в Telegram."
