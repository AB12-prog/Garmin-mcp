# Garmin Connect MCP Server

A remote **MCP server** that exposes your Garmin Connect data and workout-authoring
endpoints as tools Claude can call — the Garmin equivalent of a WHOOP connector. Read
recovery / sleep / HRV / training readiness / activities / weight, **and** build and
schedule workouts (including strength/metcon) straight onto your Garmin calendar.

It authenticates *as you* via the maintained [`garminconnect`](https://pypi.org/project/garminconnect/)
library (Garmin has no self-serve individual API). The 0.3.x line uses `curl_cffi` TLS
impersonation, which works around the Cloudflare fingerprinting that broke older tools.

---

## How it fits together

```
   auth_setup.py (run once, locally)      Railway (always-on)              claude.ai
   ┌──────────────────────────┐          ┌────────────────────┐          ┌──────────────┐
   │ log in + MFA → token blob │ ── paste │ garmin_mcp_server  │ ── URL ─ │ custom       │
   │                          │   into    │ /<SECRET>/mcp      │  add as  │ connector    │
   └──────────────────────────┘  GARMIN_  │ (Streamable HTTP)  │          │              │
                                 TOKENS    └────────────────────┘          └──────────────┘
```

Workouts you create land on Garmin Connect's calendar; you then **sync your watch** to
receive them.

There is **no "connect Garmin" button** like WHOOP has — Garmin has no individual OAuth.
Instead the account owner authenticates once and a token blob is stored as an env var.

---

## Step 1 — Mint a token (once)

Run `auth_setup.py` on any machine with a terminal (or Google Cloud Shell in a browser —
`shell.cloud.google.com` — if you're iPad-only):

```bash
pip install garminconnect
python auth_setup.py     # enter email, password, MFA code if prompted
```

It prints a long **token blob**. Copy it.

## Step 2 — Deploy to Railway

1. Push this folder to a GitHub repo (or `railway up`).
2. New Railway project from the repo. Nixpacks auto-detects Python and installs
   `requirements.txt`.
3. Add two **Variables**:
   - `GARMIN_MCP_SECRET` = a long, URL-safe random string
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   - `GARMIN_TOKENS` = the blob from Step 1 (single line, no quotes).
4. Railway sets `PORT` automatically. Under **Settings → Networking**, generate a public
   domain. Your endpoint is:

   ```
   https://<your-app>.up.railway.app/<GARMIN_MCP_SECRET>/mcp
   ```

No volume or database needed — the server is a stateless pass-through; the token blob in
the env var is the only stored state. A `GET /health` route returns `{"status":"ok"}` if
you want a Railway healthcheck.

## Step 3 — Add to Claude

In Claude (**Pro/Max required** for custom connectors): **Settings → Connectors → Add
custom connector**. Paste the full `https://…/<SECRET>/mcp` URL. No OAuth — the secret
path is the gate. Verify with `garmin_whoami`.

## Refreshing the token later (~yearly)

The token auto-refreshes and lasts about a year. If it ever expires, either re-run
`auth_setup.py` and update `GARMIN_TOKENS`, or — fully from the app — tell Claude to
reconnect: it calls `garmin_auth_start` (Garmin sends an MFA code) then
`garmin_auth_complete`. That returns a fresh blob; paste it back into `GARMIN_TOKENS` in
Railway so it survives the next restart.

---

## Hosting this for someone else

Single deployment = single Garmin account. To let (say) a sibling use it on your Railway:

- **They do one thing in their own Claude:** add the connector (paste the URL). If you set
  their `GARMIN_TOKENS` for them, they're done; otherwise they run Step 3 once to connect
  their Garmin.
- **You (the host) can access their data.** You hold the token and the URL, so you can
  read their metrics and write workouts. Only host for someone who trusts you with that,
  and share the URL over a private channel.
- **Password handling.** Prefer minting the token via `auth_setup.py` and setting
  `GARMIN_TOKENS` — then their Garmin *password* never transits a tool call. The in-app
  `garmin_auth_start` path is the fallback, and it does transmit the password to the
  server during that one call.

---

## Security notes

- **The URL is the credential.** Anyone with `https://…/<SECRET>/mcp` has full access to
  the connected account. Share it privately; rotate by changing `GARMIN_MCP_SECRET`.
- **Logs.** Access logging is set to WARNING so the secret-bearing path isn't logged.
- **The token blob is sensitive** (a live session credential). Keep it in the env var;
  never commit it. `.gitignore` covers local files.
- **Fragility.** Garmin periodically changes auth; the library is a moving target. If
  logins fail, bump `garminconnect` locally, re-mint, redeploy. Deps are pinned to the
  tested 0.3.x line to avoid a surprise breaking upgrade.

---

## Tools

**Auth** — `garmin_auth_status`, `garmin_auth_start`, `garmin_auth_complete`

**Read** — `garmin_whoami`, `garmin_daily_summary`, `garmin_sleep`, `garmin_hrv`,
`garmin_training_readiness`, `garmin_training_status`, `garmin_body_battery`,
`garmin_stress`, `garmin_recent_activities`, `garmin_activity_detail`,
`garmin_weight_trend`



**Write** — `garmin_log_weight`, `garmin_list_workouts`, `garmin_get_workout`,
`garmin_create_running_workout`, `garmin_upload_workout_json`, `garmin_schedule_workout`,
`garmin_scheduled_workouts`, `garmin_unschedule_workout`, `garmin_delete_workout`

`garmin_create_running_workout` is the main authoring tool. It takes a structured
`steps_json` array and supports:

- **Step length** by distance (`m`/`km`) or time (`s`/`min`), or lap-button.
- **Targets** per step: pace range (min:sec per km), HR zone (1-5), custom HR (bpm
  range), or none.
- **Structures**: warmup/tempo/cooldown, interval sets via nested `repeat` groups
  (e.g. 6×800m with recoveries), and pyramids/varied sessions by sequencing steps.

Workouts land on the Garmin calendar via `garmin_schedule_workout`; sync your watch to
receive them. `garmin_upload_workout_json` is an escape hatch for anything the running
builder can't express (e.g. a different sport).

---

## Security & maintenance

- **Keep the URL secret.** Anyone with the full `/<SECRET>/mcp` URL can read your data and
  write workouts. Rotate by changing `GARMIN_MCP_SECRET` and re-adding the connector.
- **Never commit `GARMIN_TOKENS`.** It's an env var only; `.gitignore` covers local files.
- **Fragility.** Garmin periodically changes its auth; the library is a moving target. If
  logins start failing, `pip install -U garminconnect` locally, re-run `auth_setup.py`,
  and redeploy. Pin a known-good version in `requirements.txt` if you want stability.
- **Token expiry (~1 year).** Re-run Step 1 and update `GARMIN_TOKENS`.

## Local run (optional)

```bash
export GARMIN_MCP_SECRET=dev-secret
export GARMIN_TOKENS='<blob>'
export PORT=8000
python garmin_mcp_server.py
# endpoint: http://127.0.0.1:8000/dev-secret/mcp
```
