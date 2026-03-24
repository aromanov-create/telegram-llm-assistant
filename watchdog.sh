#!/bin/bash
# Внешний watchdog для assistant-bot.
# Запускается cron каждые 2 минуты.
# Перезапускает сервис если:
#   1. Сервис не запущен
#   2. Бот завис (нет heartbeat более 5 минут)
#   3. Потребление памяти > 9 GB (утечка памяти)

SERVICE="assistant-bot.service"
HEARTBEAT="/tmp/assistant-bot-alive"
MAX_MEM_KB=$((9 * 1024 * 1024))  # 9 GB

log() {
    logger -t assistant-watchdog "$*"
}

restart_service() {
    log "Перезапуск: $1"
    systemctl restart "$SERVICE"
}

# 1. Сервис не запущен — systemd Restart=always должен поднять, но на всякий случай
if ! systemctl is-active --quiet "$SERVICE"; then
    restart_service "сервис не активен"
    exit 0
fi

PID=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null)

# 2. Проверка heartbeat файла
if [ -f "$HEARTBEAT" ]; then
    LAST_BEAT=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$((NOW - LAST_BEAT))
    if [ "$AGE" -gt 300 ]; then
        restart_service "event loop завис (heartbeat ${AGE}s назад)"
        exit 0
    fi
fi

# 3. Проверка памяти
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
    MEM_KB=$(cat /proc/"$PID"/status 2>/dev/null | grep VmRSS | awk '{print $2}')
    if [ -n "$MEM_KB" ] && [ "$MEM_KB" -gt "$MAX_MEM_KB" ]; then
        restart_service "утечка памяти (RSS=${MEM_KB}KB)"
        exit 0
    fi
fi
