#!/usr/bin/env bash
# Установка форка telegram-mcp на macOS и подключение к Claude Desktop.
#
#   bash setup_mac.sh            # обычный запуск
#   bash setup_mac.sh --relogin  # войти в Telegram заново (новая сессия)
#
# Что делает:
#   1. ставит uv, если его нет;
#   2. клонирует (или обновляет) форк в ~/gitprojects/telegram-mcp;
#   3. ставит зависимости (uv sync);
#   4. спрашивает api_id / api_hash и пишет их в .env (права 600);
#   5. вход в Telegram по QR и перевод сессии в файл ~/.telegram-mcp/;
#   6. добавляет сервер "telegram" в claude_desktop_config.json (с резервной копией).
#
# Ключи и сессия никуда не отправляются: они остаются в .env и ~/.telegram-mcp/.

set -euo pipefail

FORK_URL="${TG_MCP_FORK_URL:-https://github.com/h0ksie/telegram-mcp.git}"
UPSTREAM_URL="https://github.com/chigwell/telegram-mcp.git"
REPO_DIR="${TG_MCP_DIR:-$HOME/gitprojects/telegram-mcp}"
SESSION_DIR="$HOME/.telegram-mcp"
SESSION_NAME="$SESSION_DIR/telegram_mcp_session"
CLAUDE_CONFIG="${CLAUDE_CONFIG:-$HOME/Library/Application Support/Claude/claude_desktop_config.json}"
SERVER_KEY="telegram"

RELOGIN=0
[[ "${1:-}" == "--relogin" ]] && RELOGIN=1

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die() {
    printf '\033[1;31mОшибка: %s\033[0m\n' "$*" >&2
    exit 1
}

ensure_uv() {
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        say "Ставлю uv"
        if command -v brew >/dev/null 2>&1; then
            brew install uv
        else
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
    fi
    UV_BIN="$(command -v uv)" || die "uv не найден после установки"
    echo "uv: $UV_BIN"
}

fetch_repo() {
    command -v git >/dev/null 2>&1 || die "нужен git (xcode-select --install)"
    if [[ -d "$REPO_DIR/.git" ]]; then
        say "Обновляю форк в $REPO_DIR"
        git -C "$REPO_DIR" pull --ff-only
    else
        say "Клонирую форк в $REPO_DIR"
        mkdir -p "$(dirname "$REPO_DIR")"
        git clone "$FORK_URL" "$REPO_DIR"
    fi
    if ! git -C "$REPO_DIR" remote get-url upstream >/dev/null 2>&1; then
        git -C "$REPO_DIR" remote add upstream "$UPSTREAM_URL"
    fi
    cd "$REPO_DIR"
}

install_deps() {
    say "Ставлю зависимости (uv sync)"
    "$UV_BIN" sync
}

write_env() {
    if [[ -f .env ]] && grep -q '^TELEGRAM_API_ID=[0-9]' .env && grep -q '^TELEGRAM_API_HASH=' .env; then
        say ".env уже есть, ключи не трогаю"
        return
    fi
    say "Ключи Telegram API (my.telegram.org/apps → App configuration)"
    local api_id api_hash
    while true; do
        read -r -p "api_id (только цифры): " api_id
        [[ "$api_id" =~ ^[0-9]+$ ]] && break
        warn "api_id должен состоять из цифр"
    done
    while true; do
        read -r -s -p "api_hash (ввод скрыт): " api_hash
        echo
        api_hash="$(printf '%s' "$api_hash" | tr -d '[:space:]')"
        [[ "$api_hash" =~ ^[0-9a-fA-F]{32}$ ]] && break
        warn "api_hash — 32 символа 0-9 и a-f, попробуй ещё раз"
    done
    (
        umask 077
        cat >.env <<EOF
TELEGRAM_API_ID=$api_id
TELEGRAM_API_HASH=$api_hash
TELEGRAM_SESSION_NAME=$SESSION_NAME
EOF
    )
    chmod 600 .env
    echo "Записал .env (права 600)"
}

ensure_shared_session() {
    # Claude Desktop и подключённые облачные сессии запускают по своей копии сервера.
    # Обе копии делят одну сессию: ключ читается из файла, сессия живёт в памяти.
    local line
    for line in TELEGRAM_SESSION_LOCK=shared TELEGRAM_SESSION_FILE_IN_MEMORY=1; do
        grep -q "^${line%%=*}=" .env || echo "$line" >>.env
    done
}

login_telegram() {
    mkdir -p "$SESSION_DIR"
    chmod 700 "$SESSION_DIR"
    if [[ -f "$SESSION_NAME.session" && $RELOGIN -eq 0 ]]; then
        say "Сессия уже есть: $SESSION_NAME.session (новый вход: --relogin)"
        return
    fi
    if [[ -f "$SESSION_NAME.session" ]]; then
        mv "$SESSION_NAME.session" "$SESSION_NAME.session.old-$(date +%s)"
    fi
    say "Вход в Telegram по QR"
    echo "На телефоне ДОП. аккаунта: Telegram → Настройки → Устройства → Подключить устройство,"
    echo "и отсканируй QR ниже. Если включён облачный пароль — введёшь его здесь."
    # Ответы генератору: пустая метка аккаунта и "y" на запись строки сессии в .env.
    # Облачный пароль генератор спрашивает через терминал, его вводишь ты.
    printf '\ny\n' | "$UV_BIN" run session_string_generator.py --qr ||
        die "вход не удался, запусти скрипт ещё раз"
    grep -q '^TELEGRAM_SESSION_STRING=' .env || die "строка сессии не попала в .env"

    say "Перевожу сессию в файл (рекомендация автора для долгой работы)"
    "$UV_BIN" run telegram-mcp-migrate-session --target "$SESSION_NAME"
    # Строка сессии больше не нужна: убираем её из .env, остаётся только файл.
    perl -i -ne 'print unless /^TELEGRAM_SESSION_STRING=/' .env
    grep -q '^TELEGRAM_SESSION_NAME=' .env || echo "TELEGRAM_SESSION_NAME=$SESSION_NAME" >>.env
    chmod 600 "$SESSION_NAME.session"
    clear || true
    echo "Сессия сохранена в $SESSION_NAME.session."
    echo "Строку сессии с экрана я убрал; нажми Cmd+K, чтобы стереть и историю терминала."
}

update_claude_config() {
    say "Добавляю сервер \"$SERVER_KEY\" в Claude Desktop"
    export CLAUDE_CONFIG SERVER_KEY UV_BIN REPO_DIR
    "$UV_BIN" run --no-sync python - <<'PY'
import json
import os
import shutil
import sys
import time
from pathlib import Path

config = Path(os.environ["CLAUDE_CONFIG"])
data = {}
if config.exists():
    try:
        data = json.loads(config.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        sys.exit(f"Не смог прочитать {config}: {exc}. Конфиг не тронут.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = config.with_name(f"{config.name}.bak-{stamp}")
    n = 1
    while backup.exists():  # never overwrite an earlier backup
        backup = config.with_name(f"{config.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(config, backup)
    print(f"Резервная копия: {backup}")
else:
    config.parent.mkdir(parents=True, exist_ok=True)

servers = data.setdefault("mcpServers", {})
key = os.environ["SERVER_KEY"]
if key in servers:
    print(f"Сервер \"{key}\" уже был в конфиге — заменяю.")
servers[key] = {
    "command": os.environ["UV_BIN"],
    "args": ["--directory", os.environ["REPO_DIR"], "run", "main.py"],
}
config.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
others = sorted(k for k in servers if k != key)
print(f"Готово. Остальные серверы на месте: {', '.join(others) or 'нет'}")
PY
}

restart_claude() {
    echo
    read -r -p "Перезапустить Claude Desktop сейчас? [y/N] " answer
    if [[ "$answer" == [Yy]* ]]; then
        osascript -e 'quit app "Claude"' || true
        sleep 3
        open -a Claude
        echo "Claude Desktop перезапущен."
    else
        echo "Перезапусти Claude Desktop сам (Cmd+Q и открыть снова)."
    fi
}

main() {
    [[ "$(uname -s)" == "Darwin" ]] || die "скрипт для macOS"
    ensure_uv
    fetch_repo
    install_deps
    write_env
    ensure_shared_session
    login_telegram
    update_claude_config
    restart_claude
    say "Всё. Напиши в чат с Claude, что сервер подключён."
}

main "$@"
