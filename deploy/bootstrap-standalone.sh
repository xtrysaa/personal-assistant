#!/usr/bin/env bash
#
# Развёртывание БЕЗ dnf и БЕЗ git — только curl, tar и sudo.
#
# Зачем этот вариант: на VM.Standard.E2.1.Micro доступно ~500 МБ RAM, и
# `dnf install python3.11` на такой машине уходит в многоминутный свопинг
# и может подвесить sshd. Здесь системные пакеты не трогаются вообще:
#   * Python скачивается готовой сборкой (python-build-standalone);
#   * код берётся архивом с GitHub, а не через git clone.
# Подходит и для любого другого Linux (Debian, Ubuntu, RHEL-семейство —
# везде, где есть curl, tar и sudo).
#
# ПОЧЕМУ /opt, А НЕ ДОМАШНЯЯ ПАПКА
# На Oracle Linux / RHEL включён SELinux в режиме Enforcing. Файлы в /home
# помечены как user_home_t, и systemd (служебный домен) такой бинарник
# исполнить не может — сервис падает с `status=203/EXEC Permission denied`,
# хотя вручную из терминала всё запускается (там домен unconfined_t).
# В /opt контексты корректные, поэтому рантайм и приложение кладём туда.
#
# Использование на сервере:
#     bash deploy/bootstrap-standalone.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/assistant}"
PY_ROOT="${PY_ROOT:-/opt/python3.11}"
PY_SERIES="${PY_SERIES:-3.11}"
ARCH="$(uname -m)"
LIBC="unknown-linux-gnu"
REPO_TARBALL="${REPO_TARBALL:-https://github.com/xtrysaa/personal-assistant/archive/refs/heads/main.tar.gz}"
SWAP_MB="${SWAP_MB:-2048}"
OWNER="$(id -un)"

log() { printf '\n=== %s ===\n' "$1"; }

# --------------------------------------------------------------------------- #
log "1. Базовые утилиты"
for tool in curl tar sudo; do
    command -v "$tool" >/dev/null || { echo "ОШИБКА: нет $tool"; exit 1; }
done
sudo -n true 2>/dev/null || { echo "ОШИБКА: sudo требует пароль, а установка неинтерактивная"; exit 1; }
echo "curl, tar и sudo на месте ($ARCH)"

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
log "3. Python ${PY_SERIES} готовой сборкой -> $PY_ROOT"
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

    curl -fL "$TARBALL_URL" -o /tmp/python-standalone.tar.gz
    # В архиве единственный верхний каталог python/ — переносим его целиком,
    # чтобы не разбирать дерево по частям (venv потом ссылается на этот путь).
    TMP_EXTRACT="$(mktemp -d)"
    tar -xzf /tmp/python-standalone.tar.gz -C "$TMP_EXTRACT"
    sudo mkdir -p "$(dirname "$PY_ROOT")"
    sudo rm -rf "$PY_ROOT"
    sudo mv "$TMP_EXTRACT/python" "$PY_ROOT"
    rm -rf "$TMP_EXTRACT" /tmp/python-standalone.tar.gz
    echo "установлен: $("$PY_ROOT/bin/python3" --version 2>&1)"
fi
sudo chown -R "$OWNER" "$PY_ROOT"

# --------------------------------------------------------------------------- #
log "4. Код проекта архивом (без git) -> $APP_DIR"
sudo mkdir -p "$APP_DIR"
curl -fL "$REPO_TARBALL" -o /tmp/app.tar.gz
# Распаковываем поверх существующей копии: .env в архиве нет, он не затрётся
sudo tar -xzf /tmp/app.tar.gz -C "$APP_DIR" --strip-components=1
rm -f /tmp/app.tar.gz
sudo chown -R "$OWNER" "$APP_DIR"
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
log "6. Метки SELinux (иначе systemd не сможет запустить бинарник)"
if command -v getenforce >/dev/null && [ "$(getenforce)" != "Disabled" ]; then
    sudo chcon -R -t bin_t "$PY_ROOT/bin"
    sudo chcon -R -t lib_t "$PY_ROOT/lib"
    sudo restorecon -R "$APP_DIR" >/dev/null 2>&1 || true
    echo "режим: $(getenforce), метки проставлены"
    # Замечание: chcon не переживает полный relabel файловой системы.
    # Если когда-нибудь понадобится закрепить навсегда (нужен dnf):
    #   sudo semanage fcontext -a -t bin_t "$PY_ROOT/bin(/.*)?"
    #   sudo semanage fcontext -a -t lib_t "$PY_ROOT/lib(/.*)?"
    #   sudo restorecon -R "$PY_ROOT"
else
    echo "SELinux выключен или отсутствует — шаг пропущен"
fi

# --------------------------------------------------------------------------- #
log "7. Настройки"
if [ ! -f .env ]; then
    echo "ВНИМАНИЕ: нет файла $APP_DIR/.env"
    echo "Скопируй с локальной машины:"
    echo "  scp -i ключ.key .env $(id -un)@<IP>:$APP_DIR/.env"
    exit 1
fi
chmod 600 .env
echo ".env на месте (права 600)"

# --------------------------------------------------------------------------- #
log "8. Проверка логики (без сети и Telegram)"
.venv/bin/python scripts/smoke_test.py | tail -3

# --------------------------------------------------------------------------- #
log "9. Автозапуск через systemd"
if [ -f deploy/assistant-bot.service ]; then
    # В юните зашиты пути /opt/assistant и User=opc — правим под эту машину
    sed "s#/opt/assistant#$APP_DIR#g; s#^User=.*#User=$OWNER#" \
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
