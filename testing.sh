#!/usr/bin/env bash
# Auto-reloading dev server — polls for changes to .py, .html, .js, .css files

POLL_INTERVAL=1
SERVER_PID=""

start_server() {
    python brother_ql_web.py &
    SERVER_PID=$!
    echo "[reload] Server started (PID $SERVER_PID)"
}

stop_server() {
    if [ -n "$SERVER_PID" ]; then
        kill -9 "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
        SERVER_PID=""
        echo "[reload] Server stopped"
    fi
}

cleanup() {
    stop_server
    exit 0
}

trap cleanup INT TERM

get_checksum() {
    find . -name "*.py" -o -name "*.html" -o -name "*.jinja2" -o -name "*.js" -o -name "*.css" \
        | sort | xargs stat -c '%n %Y' 2>/dev/null | md5sum
}

start_server
LAST="$(get_checksum)"

while true; do
    sleep "$POLL_INTERVAL"
    CURRENT="$(get_checksum)"
    if [ "$CURRENT" != "$LAST" ]; then
        echo "[reload] Change detected, restarting..."
        stop_server
        # Wait until port 8013 is free before restarting
        for i in $(seq 1 20); do
            fuser 8013/tcp > /dev/null 2>&1 || break
            sleep 0.2
        done
        start_server
        LAST="$(get_checksum)"
    fi
done
