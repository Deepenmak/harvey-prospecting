#!/bin/sh
# Entrypoint for the Docker / Coolify deployment.
#
# The DASHBOARD is the container's main process. It must stay up no matter
# what, because it is the only way to reach the UI, sign in to Claude, and
# paste the configuration -- and none of that exists yet on a first boot.
#
# The HEARTBEAT is supervised in the background instead. `sellbird run`
# deliberately exits when the workspace isn't configured (main.py
# _needs_setup: no .env, or persona.company still "Your Company"), and it
# can't ask a wizard because there's no TTY. Running it in the foreground
# meant that exit tore down the dashboard and the container restart-looped
# every few seconds, so the UI was never reachable. It is retried instead.
set -e

HOME_DIR="${SELLBIRD_HOME:-/config}"
SEED="/app"

echo "[sellbird] Home (persistent volume): $HOME_DIR"
mkdir -p "$HOME_DIR/data" "$HOME_DIR/exports" "$HOME_DIR/skills" "$HOME_DIR/prompts"

# Seed the volume from the image on first boot. Existing files are NEVER
# overwritten, so everything configured through the UI survives a redeploy.
[ -f "$HOME_DIR/sellbird.yaml" ] || cp "$SEED/sellbird.yaml" "$HOME_DIR/sellbird.yaml"
for dir in prompts skills; do
  [ -d "$SEED/$dir" ] || continue
  for file in "$SEED/$dir"/*; do
    [ -e "$file" ] || continue
    [ -e "$HOME_DIR/$dir/$(basename "$file")" ] || cp "$file" "$HOME_DIR/$dir/"
  done
done

# The heartbeat refuses to start without a .env file. Keys set as Coolify
# environment variables are already in the process environment; this file is
# what the Settings tab writes to, and it lives on the volume.
if [ ! -f "$HOME_DIR/.env" ]; then
  (umask 077; : > "$HOME_DIR/.env")
fi
chmod 600 "$HOME_DIR/.env" 2>/dev/null || true

# A lock file left behind by an unclean stop would stop the loop starting.
rm -f "$HOME_DIR/data/sellbird.lock" "$HOME_DIR/data/sellbird.pid" 2>/dev/null || true

(
  while true; do
    python -m sellbird.cli run || true
    echo "[sellbird] Heartbeat is not running. Retrying in 60s."
    echo "[sellbird] If this repeats, finish setup in the dashboard:"
    echo "[sellbird]   Your company -> fill in company, sender and product, then Save."
    echo "[sellbird] And sign in to Claude once, from this terminal:  claude login"
    sleep 60
  done
) &

echo "[sellbird] Starting dashboard on 0.0.0.0:5555 ..."
exec python -m sellbird.cli dashboard --host 0.0.0.0 --port 5555
