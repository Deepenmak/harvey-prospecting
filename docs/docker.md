# Running Sellbird in a container

This fork deploys to a container host (Coolify, or any Docker host behind a
reverse proxy) rather than running on the operator's own machine. Everything
private and everything stateful lives on one named volume mounted at
`/config`, and the dashboard is reached over the network with a username and
password. That is a different shape from upstream Sellbird's Compose setup,
which uses host networking and a bind mount of the checkout; the notes for
that are at the end.

## What lives where

| Path | Holds | Survives a redeploy |
|---|---|---|
| `/config` (volume) | `.env`, `sellbird.local.yaml`, `skills/`, `prompts/`, `exports/` | yes |
| `/config/data` | `sellbird.db` — the lead record, the credit ledger, every run | yes |
| `/config/.claude` | the Claude login (`CLAUDE_CONFIG_DIR` points here) | yes |
| `/app` | the code, from the image | no — replaced every deploy |

`SELLBIRD_HOME=/config` is set in the image, so every path Sellbird writes is
on the volume. Nothing private is in the image or in the (public) repository.

## Signing in

The dashboard is on a public URL, so the token link printed in a terminal is
not how anyone reaches it. Set three environment variables on the deployment:

```
SELLBIRD_CLOUD_HOST=sellbird.example.com     # your hostname, no protocol, no slash
DASHBOARD_USER=<the name you sign in with>
DASHBOARD_PASS_HASH=<from: sellbird password-hash>
```

Run `sellbird password-hash` on your own machine, type the password twice, and
paste the printed `DASHBOARD_PASS_HASH` value. It contains no `$`, on purpose:
docker compose, `.env` files and most hosting UIs read `$16384` as a variable
reference, and a hash that arrives with pieces missing just looks like a
password that doesn't work. If a sign-in refuses you, run `sellbird login-check`
in a terminal on the server — it reads the same environment the dashboard does
and says which cause it is. The password itself then never
sits in the host's configuration, where anyone with access to the deployment
can read it.

`DASHBOARD_PASS` (the plain password) is read only when no hash is set, and
**the compose file deliberately doesn't name it**: Coolify pins every variable
a compose file mentions and then refuses to let you delete it, so naming it
would keep the weaker of the two set for good. To move off it:

1. Add `DASHBOARD_PASS_HASH` and redeploy. A hash always wins over the plain
   password, so sign in once to confirm the new one works — you will be asked
   to, because changing the credential ends the old sessions.
2. Redeploy on a version of this file that doesn't name `DASHBOARD_PASS`.
   Coolify will then let you delete the variable.

Until step 2 the plain value is unused but still readable in the deployment's
settings, which is the whole reason to remove it.

Without `SELLBIRD_CLOUD_HOST` the dashboard answers only to `localhost` and
the public URL returns `bad_host`. With it set and no sign-in configured, the
port is open but nobody holding a browser can get in; the startup log says so.

**Taking someone's access away: change the password.** Every sign-in records
which credential made it, so changing `DASHBOARD_PASS_HASH` (or `DASHBOARD_PASS`,
or `DASHBOARD_USER`) ends every sign-in made under the old one the moment the new
value is live — nobody has to remember to revoke a session. Redeploying with the
same values leaves sign-ins alone. **Settings → Who is signed in** lists them and
has **Sign everyone out**.

A sign-in lasts **7 days**, or **12 hours unused**, whichever comes first, and
**Sign out** in the sidebar ends it. Sessions are listed in
`/config/data/sessions.json` as hashes — the tokens themselves are never
stored — and survive a redeploy. `DELETE /api/auth/sessions` signs every
browser out at once. Five wrong passwords from one address, or twenty-five in
all, stop sign-in attempts for fifteen minutes.

Each sign-in gets its own token. It is not the token in
`/config/data/dashboard-token`: that one is the machine credential the CLI and
the MCP server use, it never expires, and no password can hand it out.

## Claude

`claude login` once, from a terminal on the running container (Coolify:
**Terminal** on the resource). It writes to `/config/.claude`, so it is done
once and not per deploy. Until it is done, the heartbeat can read websites but
writes nothing, and **Today** says why.

## First run

1. Deploy. The container starts the dashboard; the heartbeat is started from
   its **Controls** card, not automatically.
2. Open the public URL and sign in.
3. **Your company** → fill it in and save. The image ships the blank template,
   so until this is done the loop refuses to start and Today says so.
4. **Settings** → API keys (Apollo, and a mailbox). They are written to
   `/config/.env` on the volume.
5. `claude login` in the container's terminal.
6. **Controls** → start Sellbird.

## Upgrading

Redeploy. The image is replaced and the volume is not, so the database,
credentials, trained config and Claude login all carry over. Back up
`/config` (the whole volume) before moving the installation anywhere.

The dashboard listens on 5555 and the Compose file publishes it for the proxy
to terminate TLS in front of. If you change the port, change the Dockerfile's
health check to match and rebuild.

## Upstream's local Compose setup

Upstream Sellbird runs Compose on the operator's own Linux machine with
`network_mode: host`, the checkout bind-mounted at `/app`, no published port,
and the dashboard reached through an SSH tunnel
(`ssh -L 5555:127.0.0.1:5555 <host>`). That remains the right shape for a
single operator who wants nothing exposed: there is no password to manage,
because there is nothing to reach. It is not what this fork's
`docker-compose.yml` does. Use a host install if you need the Codex provider —
the image ships the Claude CLI only.
