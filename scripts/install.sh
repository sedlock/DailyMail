#!/usr/bin/env bash
# Install DailyMail's checked-in user units without running DailyMail work.
#
# This script has no configurable repository path by design. It runs only from
# the authoritative DailyMail checkout, which is the path written into the
# systemd unit by `dailymail install-timer`.

set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
CANONICAL_REPOSITORY_DIR="/mnt/bench/src/DailyMail"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_UNIT="$UNIT_DIR/dailymail.service"
TIMER_UNIT="$UNIT_DIR/dailymail.timer"

usage() {
    cat <<'EOF'
Usage: ./scripts/install.sh [install|--status|--remove]

  install   Synchronize locked dependencies and install/reload DailyMail user
            units without starting or enabling the timer. (Default.)
  --status  Print read-only DailyMail health JSON and exact user-unit status.
  --remove  Disable/remove DailyMail user units only; preserve all state and
            credentials.
EOF
}

require_repository() {
    if [[ "$REPOSITORY_DIR" != "$CANONICAL_REPOSITORY_DIR" ]]; then
        echo "DailyMail installer must run from $CANONICAL_REPOSITORY_DIR." >&2
        exit 2
    fi
    if [[ ! -f "$REPOSITORY_DIR/pyproject.toml" || ! -f "$REPOSITORY_DIR/uv.lock" ]]; then
        echo "DailyMail repository files are missing beside this script." >&2
        exit 2
    fi
}

install_units() {
    require_repository
    cd "$REPOSITORY_DIR"
    uv sync --frozen
    # --no-enable is intentional: Persistent=true can run a missed scheduled
    # occurrence as soon as a timer starts. Deploying must never invoke Rowan
    # retrieval or send email as an indirect side effect.
    uv run --frozen dailymail install-timer --no-enable
    cat <<EOF
INSTALL OK
  repository: $REPOSITORY_DIR
  units: $SERVICE_UNIT and $TIMER_UNIT
  state preserved: ${XDG_DATA_HOME:-$HOME/.local/share}/dailymail
  credentials preserved: ${XDG_CONFIG_HOME:-$HOME/.config}/dailymail/credentials.env
  scheduling: existing enabled timers remain enabled; a new timer is not started by this script.
EOF
}

status() {
    require_repository
    cd "$REPOSITORY_DIR"
    # --no-sync makes this inspection path non-mutating; install first if the
    # local virtual environment does not yet exist.
    uv run --frozen --no-sync dailymail health --json
    systemctl --user status dailymail.service dailymail.timer --no-pager --full
}

remove_units() {
    # Disabling the timer stops scheduling only. It deliberately does not stop
    # a currently executing oneshot service or remove any DailyMail data.
    systemctl --user disable --now dailymail.timer >/dev/null 2>&1 || true
    rm -f -- "$SERVICE_UNIT" "$TIMER_UNIT"
    systemctl --user daemon-reload
    cat <<EOF
REMOVE OK
  removed units: $SERVICE_UNIT and $TIMER_UNIT
  preserved state: ${XDG_DATA_HOME:-$HOME/.local/share}/dailymail
  preserved credentials: ${XDG_CONFIG_HOME:-$HOME/.config}/dailymail/credentials.env
EOF
}

case "${1:-install}" in
    install)
        [[ $# -eq 0 || $# -eq 1 ]] || { usage >&2; exit 2; }
        install_units
        ;;
    --status)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        status
        ;;
    --remove)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        remove_units
        ;;
    -h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
