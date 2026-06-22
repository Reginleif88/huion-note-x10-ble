#!/usr/bin/env bash
# huion-x10-notes.sh - launcher for the Huion Note X10 offline note extractor.
#
# Runs `python3 -m huion_notes <args>` with the package importable from any CWD.
#
# For the live `dump` it does two things the bare CLI can't:
#   1. Pauses the pen/tablet driver user service if it is running. The driver
#      holds the BLE link in pen-tablet mode and would fight the dump for the
#      one connection. The service is RESTARTED afterward (only if it was
#      running, and even if the dump fails or you Ctrl-C it).
#   2. Ensures `dbus_fast` is available: the active python if it already has it,
#      otherwise a Nix-provided python, otherwise pip/venv guidance.
#
# `decode` / `--help` never touch the device, so the driver is left alone and no
# dbus_fast is needed. Output paths are relative to YOUR current directory.
#
# Usage:
#   ./huion-x10-notes.sh decode capture.btsnoop -o ./notes-out
#   ./huion-x10-notes.sh dump -o ./notes-out [--mac AA:BB:..] [--pin 123456] [--verbose]
set -euo pipefail

# Resolve this script's real location (follow symlinks) so the huion_notes
# package and huion_ble_driver.py import via PYTHONPATH from any CWD.
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SELF")"
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

# Pen-driver user services to pause during a dump. The repo ships the first two;
# the NixOS module installs `huion-ble`. Add your unit name here if it differs.
DRIVER_SERVICES=(huion-note-x10-portrait huion-note-x10-landscape huion-ble)

# First non-option argument = the subcommand (decode | dump).
sub=""
for a in "$@"; do case "$a" in -*) ;; *) sub="$a"; break ;; esac; done

# Default output folder. Override per-run with -o/--out, or globally with the
# HUION_NOTES_OUT env var. Edit this default to point wherever you keep notes.
OUT_DIR="${HUION_NOTES_OUT:-./notes-out}"

# For decode/dump, if no -o/--out was given, inject the default OUT_DIR so you
# can run `... dump` (or `... decode FILE`) without repeating -o every time.
cli_args=("$@")
has_out=0
for a in "$@"; do case "$a" in -o|-o?*|--out|--out=*) has_out=1; break ;; esac; done
if [ "$has_out" -eq 0 ] && { [ "$sub" = "dump" ] || [ "$sub" = "decode" ]; }; then
  cli_args+=(-o "$OUT_DIR")
fi

# Pick a Python that can import dbus_fast for the live dump: a repo-local .venv
# (./.venv next to this script) first, then the active python3. Prints the
# interpreter, or nothing if neither has dbus_fast. The .venv is auto-detected
# so non-Nix users never have to activate it — create it once and forget it.
_python_with_dbus() {
  local py
  for py in "$SCRIPT_DIR/.venv/bin/python3" python3; do
    if "$py" -c 'import dbus_fast' >/dev/null 2>&1; then
      printf '%s' "$py"
      return 0
    fi
  done
  return 1
}

# Run the CLI under an interpreter that has dbus_fast. Does NOT exec, so the
# caller can run cleanup afterward. Returns the CLI's exit status.
run_cli() {
  local py
  if py="$(_python_with_dbus)"; then
    "$py" -m huion_notes "$@"
  elif command -v nix-shell >/dev/null 2>&1; then
    echo "huion-x10-notes: providing dbus_fast via nix-shell (first run may take a moment)..." >&2
    nix-shell -p "python3.withPackages(ps: [ps.dbus-fast])" \
      --run "PYTHONPATH=$(printf %q "$PYTHONPATH") python3 -m huion_notes $(printf '%q ' "$@")"
  else
    cat >&2 <<'EOF'
huion-x10-notes: the `dump` command needs the dbus_fast Python package.
  Any distro (venv):  cd <repo> && python3 -m venv .venv && .venv/bin/pip install dbus-fast
                      (the launcher auto-detects ./.venv next to it — no need to activate it)
  NixOS / nix:        runs automatically when `nix-shell` is on PATH.
EOF
    return 1
  fi
}

# decode / --help / anything that isn't a live dump: no device, no driver
# conflict, no dbus_fast needed -> run directly.
if [ "$sub" != "dump" ]; then
  exec python3 -m huion_notes "${cli_args[@]}"
fi

# --- dump: pause the pen driver (it owns the BLE link in pen mode), then restore ---
stopped=()
if command -v systemctl >/dev/null 2>&1; then
  for svc in "${DRIVER_SERVICES[@]}"; do
    if systemctl --user is-active --quiet "$svc" 2>/dev/null; then
      echo "huion-x10-notes: pausing pen-driver service '$svc' for the dump..." >&2
      if systemctl --user stop "$svc" 2>/dev/null; then
        stopped+=("$svc")
      else
        echo "huion-x10-notes: warning: could not stop '$svc' (continuing anyway)." >&2
      fi
    fi
  done
fi

restore() {
  for svc in "${stopped[@]:-}"; do
    [ -n "$svc" ] || continue
    echo "huion-x10-notes: restarting pen-driver service '$svc'..." >&2
    systemctl --user start "$svc" 2>/dev/null \
      || echo "huion-x10-notes: warning: could not restart '$svc' — start it manually." >&2
  done
}
# Restore on any exit, including Ctrl-C / SIGTERM during a long dump.
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

rc=0
run_cli "${cli_args[@]}" || rc=$?
exit "$rc"
