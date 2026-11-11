#!/usr/bin/env bash
# Prepares a Claude cloud session for the scheduled lead fetch. Runs at
# session start through the SessionStart hook in .claude/settings.json, so
# Sellbird is installed before the routine's prompt is read. Does nothing on a
# laptop (CLAUDE_CODE_REMOTE is only set in cloud sessions).
#
# Anything printed here is shown to Claude as session context, so it reports
# problems in plain words and never prints a key.

[ -n "$CLAUDE_CODE_REMOTE" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}" || exit 0

# The cloud image's bare `pip` belongs to a different Python than `python`,
# so always install through `python -m pip`.
if ! python -c "import sellbird, httpx, pytest" 2>/dev/null; then
  python -m pip install -q --disable-pip-version-check -e . pytest pytest-asyncio \
    >/tmp/sellbird_setup.log 2>&1
fi

if python -c "import sellbird, httpx, pytest" 2>/dev/null; then
  echo "Cloud setup: Sellbird is installed for $(python --version 2>&1)."
else
  echo "Cloud setup FAILED: Sellbird could not be installed. See /tmp/sellbird_setup.log."
fi

if [ -z "$APOLLO_HARVEY_KEY" ]; then
  echo "Cloud setup: APOLLO_HARVEY_KEY is not set in the cloud environment."
fi

# Same client and proxy settings Sellbird uses: a refused proxy CONNECT raises,
# any HTTP status means the host is reachable.
if ! python -c "import httpx; httpx.get('https://api.apollo.io/', timeout=15)" 2>/dev/null; then
  echo "Cloud setup: api.apollo.io is blocked by this environment's network" \
       "policy. Set Network access to Full in the environment settings."
fi
exit 0
