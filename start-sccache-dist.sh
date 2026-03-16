#!/bin/bash
# start-sccache-dist.sh - Start the sccache-dist scheduler for gooey workflow
# This script launches the sccache-dist scheduler on the host, enabling
# distributed compilation across all agent containers.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/sccache-dist.toml"
CACHE_DIR="/home/mrwilson/.cache/sccache-dist"
LOG_FILE="${CACHE_DIR}/sccache-dist-scheduler.log"
PID_FILE="${CACHE_DIR}/sccache-dist-scheduler.pid"
PORT=10600

# Parse command line arguments
COMMAND="${1:-start}"

usage() {
    echo "Usage: $0 {start|stop|status|restart|logs}"
    echo ""
    echo "Commands:"
    echo "  start   - Start the sccache-dist scheduler (default)"
    echo "  stop    - Stop the running scheduler"
    echo "  status  - Check if scheduler is running"
    echo "  restart - Restart the scheduler"
    echo "  logs    - Show scheduler logs"
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
        echo "  cargo install sccache --locked"
        exit 1
    fi
}

start_scheduler() {
    ensure_cache_dir
    check_sccache_installed

    if [ -f "${PID_FILE}" ]; then
        PID=$(cat "${PID_FILE}")
        if kill -0 "${PID}" 2>/dev/null; then
            echo "sccache-dist scheduler is already running (PID: ${PID})"
            return 0
        else
            echo "Removing stale PID file"
            rm -f "${PID_FILE}"
        fi
    fi

    # Check if sccache-dist binary exists (separate from sccache)
    if command -v sccache-dist &> /dev/null; then
        echo "Starting sccache-dist scheduler..."
        echo "  Config: ${CONFIG_FILE}"
        echo "  Cache dir: ${CACHE_DIR}"
        echo "  Port: ${PORT}"
        echo "  Log file: ${LOG_FILE}"

        # Export environment variables
        export SCCACHE_DIR="${CACHE_DIR}"
        export SCCACHE_CACHE_SIZE="10737418240"  # 10GB

        # Start sccache-dist scheduler with config file
        # Note: sccache-dist uses 'scheduler' subcommand
        nohup sccache-dist scheduler --config "${CONFIG_FILE}" \
            > "${LOG_FILE}" 2>&1 &

        sleep 2

        # Get the scheduler PID
        SCHEDULER_PID=$(pgrep -f "sccache-dist scheduler" | head -1)

        if [ -n "${SCHEDULER_PID}" ]; then
            echo "${SCHEDULER_PID}" > "${PID_FILE}"
            echo "sccache-dist scheduler started successfully (PID: ${SCHEDULER_PID})"
            return 0
        else
            echo "Warning: Scheduler may not have started correctly. Check logs:"
            echo "  tail -f ${LOG_FILE}"
            return 1
        fi
    else
        echo "Note: sccache-dist binary not found."
        echo ""
        echo "The sccache-dist scheduler is a separate binary from sccache."
        echo "To build and install it:"
        echo ""
        echo "  .tools/install-sccache-dist.sh"
        echo ""
        echo "For distributed compilation across containers, you need:"
        echo "  1. sccache-dist scheduler (this script)"
        echo "  2. Containers with SCCACHE_DIST_SCHEDULER_URL set"
        echo ""
        return 1
    fi
}

stop_scheduler() {
    if [ -f "${PID_FILE}" ]; then
        PID=$(cat "${PID_FILE}")
        if kill -0 "${PID}" 2>/dev/null; then
            echo "Stopping sccache-dist scheduler (PID: ${PID})..."
            sccache --stop-server 2>/dev/null || kill "${PID}" 2>/dev/null || true
            rm -f "${PID_FILE}"
            echo "Scheduler stopped"
            return 0
        else
            echo "Scheduler not running (stale PID file)"
            rm -f "${PID_FILE}"
            return 1
        fi
    else
        if sccache --stop-server 2>/dev/null; then
            echo "Scheduler stopped"
            return 0
        fi
        echo "No running scheduler found"
        return 1
    fi
}

show_status() {
    echo "sccache-dist scheduler status:"
    echo ""

    if pgrep -f "sccache" > /dev/null 2>&1; then
        SCCACHE_PID=$(pgrep -f "sccache" | head -1)
        echo "  Status: RUNNING (PID: ${SCCACHE_PID})"
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
    sccache --show-stats 2>/dev/null || echo "  (scheduler not running, cannot show stats)"

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
        start_scheduler
        ;;
    stop)
        stop_scheduler
        ;;
    status)
        show_status
        ;;
    restart)
        stop_scheduler
        sleep 1
        start_scheduler
        ;;
    logs)
        show_logs
        ;;
    *)
        usage
        ;;
esac
