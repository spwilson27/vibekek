#!/bin/bash
# start-sccache.sh - Start the sccache server for gooey workflow
# This script launches the sccache server on the host, making it available
# to all agent containers for shared Rust build caching.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/sccache-config.toml"
CACHE_DIR="/home/mrwilson/.cache/sccache"
LOG_FILE="${CACHE_DIR}/sccache-server.log"
PID_FILE="${CACHE_DIR}/sccache-server.pid"
PORT=6301

# Parse command line arguments
COMMAND="${1:-start}"

usage() {
    echo "Usage: $0 {start|stop|status|restart|logs}"
    echo ""
    echo "Commands:"
    echo "  start   - Start the sccache server (default)"
    echo "  stop    - Stop the running sccache server"
    echo "  status  - Check if server is running and show stats"
    echo "  restart - Restart the sccache server"
    echo "  logs    - Show server logs"
    exit 1
}

ensure_cache_dir() {
    if [ ! -d "${CACHE_DIR}" ]; then
        echo "Creating cache directory: ${CACHE_DIR}"
        mkdir -p "${CACHE_DIR}"
    fi
}

check_sccache_installed() {
    if ! command -v sccache &> /dev/null; then
        echo "Error: sccache not found. Please install sccache first:"
        echo "  cargo install sccache"
        echo "  # or on Ubuntu: apt-get install sccache"
        echo "  # or on macOS: brew install sccache"
        exit 1
    fi
}

start_server() {
    ensure_cache_dir
    check_sccache_installed
    
    if [ -f "${PID_FILE}" ]; then
        PID=$(cat "${PID_FILE}")
        if kill -0 "${PID}" 2>/dev/null; then
            echo "sccache server is already running (PID: ${PID})"
            return 0
        else
            echo "Removing stale PID file"
            rm -f "${PID_FILE}"
        fi
    fi
    
    echo "Starting sccache server..."
    echo "  Cache dir: ${CACHE_DIR}"
    echo "  Port: ${PORT}"
    echo "  Log file: ${LOG_FILE}"
    echo "  Host: 0.0.0.0 (all interfaces)"

    # Export environment variables for sccache configuration
    # sccache reads these env vars for configuration
    export SCCACHE_DIR="${CACHE_DIR}"
    export SCCACHE_CACHE_SIZE="10737418240"  # 10GB
    export SCCACHE_SERVER_PORT="${PORT}"
    export SCCACHE_SERVER_ADDR="0.0.0.0"  # Bind to all interfaces for container access

    # Start sccache server in background
    sccache --start-server \
        > "${LOG_FILE}" 2>&1
    
    sleep 2
    
    # Get the actual sccache daemon PID (sccache forks itself)
    SCCACHE_PID=$(pgrep -f "sccache" | head -1)
    
    if [ -n "${SCCACHE_PID}" ]; then
        echo "${SCCACHE_PID}" > "${PID_FILE}"
        echo "sccache server started successfully (PID: ${SCCACHE_PID})"
        return 0
    else
        echo "Warning: Server may not have started correctly. Check logs:"
        echo "  tail -f ${LOG_FILE}"
        return 1
    fi
}

stop_server() {
    if [ -f "${PID_FILE}" ]; then
        PID=$(cat "${PID_FILE}")
        if kill -0 "${PID}" 2>/dev/null; then
            echo "Stopping sccache server (PID: ${PID})..."
            sccache --stop-server 2>/dev/null || kill "${PID}" 2>/dev/null || true
            rm -f "${PID_FILE}"
            echo "Server stopped"
            return 0
        else
            echo "Server not running (stale PID file)"
            rm -f "${PID_FILE}"
            return 1
        fi
    else
        # Try to stop by asking sccache directly
        if sccache --stop-server 2>/dev/null; then
            echo "Server stopped"
            return 0
        fi
        echo "No running sccache server found"
        return 1
    fi
}

show_status() {
    echo "sccache server status:"
    echo ""

    # Check if sccache daemon is running via pgrep
    if pgrep -f "sccache" > /dev/null 2>&1; then
        SCCACHE_PID=$(pgrep -f "sccache" | head -1)
        echo "  Status: RUNNING (PID: ${SCCACHE_PID})"
        # Update PID file
        echo "${SCCACHE_PID}" > "${PID_FILE}"
    else
        if [ -f "${PID_FILE}" ]; then
            echo "  Status: NOT RUNNING (stale PID file)"
            rm -f "${PID_FILE}"
        else
            echo "  Status: NOT RUNNING (no PID file)"
        fi
    fi

    echo ""
    echo "Cache statistics:"
    sccache --show-stats 2>/dev/null || echo "  (server not running, cannot show stats)"

    echo ""
    echo "Cache directory: ${CACHE_DIR}"
    if [ -d "${CACHE_DIR}" ]; then
        DU=$(du -sh "${CACHE_DIR}" 2>/dev/null | cut -f1)
        echo "  Size: ${DU}"
    else
        echo "  (not created yet)"
    fi
}

show_logs() {
    if [ -f "${LOG_FILE}" ]; then
        tail -50 "${LOG_FILE}"
    else
        echo "No log file found: ${LOG_FILE}"
    fi
}

case "${COMMAND}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    status)
        show_status
        ;;
    restart)
        stop_server
        sleep 1
        start_server
        ;;
    logs)
        show_logs
        ;;
    *)
        usage
        ;;
esac
