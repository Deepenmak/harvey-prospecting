# Troubleshooting

## Troubleshooting

**`command not found: sellbird`** — `source .venv/bin/activate` first.

**`ModuleNotFoundError: No module named 'sellbird'` after install (macOS)** — Python 3.13 silently ignores `.pth` files carrying the macOS hidden flag, and some Macs propagate that flag into `.venv`. Run `sellbird install` again (it auto-fixes), or: `ln -s "$(pwd)/sellbird" .venv/lib/python3.13/site-packages/sellbird`

**`externally-managed-environment`** — Use a venv, not system Python.

**`SyntaxError` or odd `ImportError`s right after install (macOS)** — Your venv is Python 3.9. `python3 -m venv` builds from `/usr/bin/python3`, which macOS keeps at 3.9. Check with `.venv/bin/python -V`, then rebuild: `rm -rf .venv && python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .`

**Claude headless mode fails** — `claude login`, and confirm the subscription is active. Test with `claude -p "say hi"`. In Docker, mount `~/.claude` into the container (writable: the CLI refreshes its login there). If the log says the Claude CLI "lacks --safe-mode" (or another flag), run `claude update` (in Docker, rebuild the image); Sellbird resumes on its own. If your Claude login depends on `env` or `apiKeyHelper` entries in your Claude settings files, set those variables in Sellbird's environment instead: Sellbird's calls don't read settings files. `sellbird status --check` confirms the CLI without sending a prompt.

**The pipeline is stuck at zero and nothing is running** — Open **Today**: if anything is in the way, the pipeline card says so in a red banner at the top ("Sellbird can't run: …"), the Controls tab repeats it with a link to the fix, and the sidebar replaces "Sellbird is stopped" with the reason. The usual three are: the config still says "Your Company" (the heartbeat refuses to start until **Your company** is filled in and saved — a container gets the blank template, not your local file), the Claude CLI is missing or too old, or Claude isn't signed in. Note which step is stuck: reading websites makes no model call, so if *that* is at zero the loop isn't completing cycles at all, whereas drafts alone at zero points at Claude.

**Claude signs out after a redeploy (Docker/Coolify)** — The image puts `CLAUDE_CONFIG_DIR` on the `/config` volume, so `claude login` survives. On an older image it went to `$HOME` inside the container and was lost with it; rebuild, then sign in once more.

**The dashboard password doesn't work** — Run `sellbird login-check` in a terminal on the server (Coolify: **Terminal** on the resource). It reads the same environment the dashboard reads and says which of the look-alike causes it is: the variable never arrived, the hash was altered on the way in, the username differs, or nothing is configured at all. `sellbird login-check --password` also checks a password against what is set. It prints no secret. Note that `sellbird password-hash` now prints a hash with no `$` in it — a `$` is read as a variable reference by docker compose, `.env` files and most hosting UIs, so an older hash could arrive with pieces missing; the older form is still accepted, including the `$$` form one round of escaping leaves behind.

**Locked out of the dashboard completely** — The master token still works and no password can disable it. In a terminal on the server, `cat data/dashboard-token` (on a container deployment, `/config/data/dashboard-token`) and paste it into the dashboard's unlock box. Then fix the sign-in with `sellbird login-check`.

**"Too many failed sign-ins"** — Five wrong passwords from one address, or twenty-five in all, stop sign-in for fifteen minutes; the reply says how long. It stops the right password too, on purpose. Waiting is the only way through: restarting the container clears the count, but if you can restart it you can also read `DASHBOARD_USER`.

**Signed out unexpectedly** — Changing `DASHBOARD_USER`, `DASHBOARD_PASS` or `DASHBOARD_PASS_HASH` ends every sign-in made under the old value, on purpose: that is how access is taken away. Otherwise a sign-in lasts 7 days, or 12 hours unused. `data/sessions.json` holding them is on the volume, so a redeploy keeps them; a *new* volume, or `DELETE /api/auth/sessions`, ends them all. Signing in again returns you to the tab you were on.

**Back goes somewhere unexpected** — Every tab is a URL (`#/outbox`), so Back moves between the tabs you visited. It no longer lands on the sign-in form: `/login` now replaces its own history entry instead of leaving one behind, and visiting it while already signed in goes straight through.

**Dashboard says "locked"** — Open the link `sellbird dashboard` printed (or `sellbird dashboard --url`). If it says the token file can't be used, follow the message: usually delete `data/dashboard-token` and restart the dashboard. Visit it as `127.0.0.1` or `localhost`; other host names are refused.

**Codex calls fail** — Read the log line; every Codex error says what to do. "Not logged in" or "expired or is due for renewal" → use Codex once or run `codex login` with ChatGPT. "Hasn't been verified with Sellbird" → install the supported Codex version named in the message. "Isolation could not be verified" → a Codex config layer outside your `~/.codex` (for example a managed or `/etc/codex` config) forces a tool, hook, MCP server, instructions or custom endpoint back on; Sellbird refuses to send prompts until it's gone. Model catalog errors → choose an available model ID from the catalog shown in the message, or leave `brain.model` blank to select a listed model. Sellbird never switches to Claude on its own.

**Discovery finds nothing** — Confirm signals first (`sellbird signals --confirm free`); nothing is collected until you do. If you're on the free OpenStreetMap source, it only covers mapped trades and is thin for service-area businesses — `sellbird discover --providers` shows the alternatives.

**Overpass is throttling** — It's free volunteer infrastructure. Sellbird walks three mirrors before giving up. Wait a few minutes, or use a paid provider for bulk work.

**Emails land in spam** — Almost always the domain, not the copy. Check SPF/DKIM/DMARC, confirm warmup ran ([TrulyInbox](https://trulyinbox.com/?via=sellbird) shows the inbox rate; `sellbird warmup` reads it once connected), halve your volume.

**Sellbird does nothing during the day** — Check `quiet_hours` and whether it hit `max_daily_claude_percent`. `sellbird status` shows current state; the `actions` table logs every decision.

**How do I stop it right now?** — `Ctrl+C`, or `sellbird sending pause` to stop outbound while leaving the loop running. State is in SQLite, so it resumes cleanly.

**Where does my data live?** — All local: `data/sellbird.db`, `.env`, `sellbird.yaml`. Nothing goes anywhere except the APIs you configured.
