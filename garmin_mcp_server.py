"""
Garmin Connect MCP server.

A remote MCP server (Streamable HTTP) that exposes your Garmin Connect data and
running-workout authoring as tools Claude can call. Designed to be deployed on Railway
and added to Claude as a custom connector, mirroring a WHOOP-style setup.

Auth model
----------
Garmin has no self-serve individual API, so this server authenticates AS the account
owner using the maintained `garminconnect` library (0.3.x, curl_cffi transport). Provide
a token, either by signing in from Claude (garmin_auth_start / garmin_auth_complete) or by
seeding the GARMIN_TOKENS env var once (mint it with auth_setup.py).

Garmin rotates the refresh token every time the session refreshes, so the *current* token
must be saved each time it changes — otherwise a restart reloads a used-up token and the
user has to sign in again. The server therefore keeps the live token in a file on a Railway
volume (GARMIN_TOKEN_PATH, default /data/garmin_tokens.json), loads it on boot, and lets
the library re-save it on every refresh. GARMIN_TOKENS is only a first-boot seed.

Connector security
------------------
Custom connectors added by URL don't forward any secret to the server, so the endpoint is
gated by a hard-to-guess PATH: the MCP endpoint lives at /<GARMIN_MCP_SECRET>/mcp, and any
other path 404s. Set GARMIN_MCP_SECRET to a long, URL-safe random string and share the URL
privately — anyone holding it has full access to the connected account. Access logging is
set to WARNING so the secret-bearing path is never written to logs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from garminconnect import Garmin

from cache import FRESH_MINUTES, Store, Syncer
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PORT = int(os.getenv("PORT", "8000"))
SECRET = os.getenv("GARMIN_MCP_SECRET", "").strip()
# First-boot seed only: a token blob minted with auth_setup.py. The live token is kept in
# TOKEN_PATH, because Garmin rotates refresh tokens and a static env blob goes stale.
ENV_TOKENS = os.getenv("GARMIN_TOKENS", "").strip()
# Where the live token is persisted. Point it at a Railway volume so it survives restarts.
TOKEN_PATH = os.getenv("GARMIN_TOKEN_PATH", "").strip() or (
    "/data/garmin_tokens.json" if os.path.isdir("/data") else ""
)
# Local data store (same volume). Without a volume it falls back to memory: still fast
# within a process, but rebuilt after each restart.
DB_PATH = os.getenv("GARMIN_DB_PATH", "").strip() or (
    "/data/garmin.db" if os.path.isdir("/data") else ":memory:"
)
# Optional IANA zone (e.g. "Australia/Brisbane") so "today" means the user's day, not UTC.
LOCAL_TZ = os.getenv("LOCAL_TIMEZONE", "").strip()

if not SECRET:
    # Fail loudly rather than silently exposing an open endpoint to health data.
    raise SystemExit(
        "GARMIN_MCP_SECRET is not set. Set it to a long, URL-safe random string; the MCP "
        "endpoint is served at /<GARMIN_MCP_SECRET>/mcp."
    )
if "/" in SECRET or any(c.isspace() for c in SECRET):
    raise SystemExit("GARMIN_MCP_SECRET must be URL-safe (no slashes or whitespace).")

mcp = FastMCP(
    "Garmin Connect",
    instructions=(
        "Tools for reading Garmin Connect health/training data and for creating and "
        "scheduling running workouts. If a tool reports the server isn't authenticated, "
        "guide the user through garmin_auth_start then garmin_auth_complete. Use the read "
        "tools for recovery, sleep, HRV, training readiness/status, activities and weight "
        "trends. Use garmin_create_running_workout to build structured sessions and "
        "garmin_schedule_workout to place them on the Garmin calendar; the user then syncs "
        "their watch. Dates are ISO 'YYYY-MM-DD'."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    # WARNING drops uvicorn's INFO access log, which would otherwise write the
    # secret-bearing request path to Railway logs on every call.
    log_level="WARNING",
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe. Intentionally reveals nothing about auth state or the secret."""
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------- #
# Garmin client — lazy, cached, thread-serialized, re-login on auth failure
# --------------------------------------------------------------------------- #
class NotAuthenticated(Exception):
    """Raised when no Garmin token is available yet."""


_client: Garmin | None = None
_pending: dict[str, Any] = {}  # holds the Garmin instance between MFA steps
_state_lock = threading.Lock()  # guards _client / _token / _pending mutations
_call_lock = threading.RLock()  # serializes access to the (non-thread-safe) HTTP client


def _persistent() -> bool:
    """True when a writable token file is configured (i.e. sign-ins survive restarts)."""
    if not TOKEN_PATH:
        return False
    try:
        os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
        return os.access(os.path.dirname(TOKEN_PATH) or ".", os.W_OK)
    except OSError:
        return False


def _read_token_file() -> str:
    if TOKEN_PATH and os.path.isfile(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return ""


def _initial_token() -> str:
    """Saved token first (it is the most recent); fall back to the env seed."""
    return _read_token_file() or ENV_TOKENS


_token: str = _initial_token()


def _bind_persistence(g: Garmin) -> None:
    """Save the client's current token and have the library re-save it on every refresh."""
    if not _persistent():
        return
    try:
        g.client._tokenstore_path = TOKEN_PATH  # library auto-dumps here after refreshes
        g.client.dump(TOKEN_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[garmin] could not persist token: {type(exc).__name__}", flush=True)


def _remember(g: Garmin) -> None:
    """Keep the in-memory copy of the token current (it rotates on refresh), so a
    reconnect never falls back to a used-up token even without a volume."""
    global _token
    try:
        current = g.client.dumps()
    except Exception:  # noqa: BLE001
        return
    if current and current != _token:
        with _state_lock:
            _token = current


def _connect() -> Garmin:
    if not _token:
        raise NotAuthenticated(
            "Garmin isn't connected. Ask the account owner for their Garmin email and "
            "password and call garmin_auth_start; if an MFA code is requested, follow up "
            "with garmin_auth_complete."
        )
    client = Garmin()
    client.login(_token)  # accepts the token JSON directly
    _bind_persistence(client)
    _remember(client)
    return client


def gc() -> Garmin:
    global _client
    with _state_lock:
        client = _client
    if client is None:
        client = _connect()
        with _state_lock:
            _client = client
    return client


def _is_auth_error(exc: Exception) -> bool:
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(k in msg for k in ("401", "unauthorized", "authentication", "token", "login"))


def _call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a Garmin client method under a serializing lock. On an auth error, rebuild
    the client once from the newest saved token. Serialization protects the shared
    curl_cffi session from concurrent tool calls."""
    global _client, _token
    with _call_lock:
        try:
            client = gc()
            result = getattr(client, fn_name)(*args, **kwargs)
            _remember(client)
            return result
        except NotAuthenticated:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_auth_error(exc):
                raise
            with _state_lock:
                _client = None
                _token = _read_token_file() or _token
            try:
                client = gc()
                result = getattr(client, fn_name)(*args, **kwargs)
                _remember(client)
                return result
            except Exception as exc2:  # noqa: BLE001
                if _is_auth_error(exc2):
                    raise NotAuthenticated(
                        "The Garmin session has expired. Sign in again with "
                        "garmin_auth_start (and garmin_auth_complete if Garmin sends a code)."
                    ) from exc2
                raise


def _today() -> str:
    if LOCAL_TZ:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(LOCAL_TZ)).date().isoformat()
        except Exception:  # noqa: BLE001 — bad zone name or no tzdata: fall back
            pass
    return date.today().isoformat()


store = Store(DB_PATH)
syncer = Syncer(store, lambda name, *a, **k: _call(name, *a, **k), lambda: _today())


def _dump(obj: Any, max_chars: int = 60_000) -> str:
    """Compact JSON with a size guard so we never blow up the context window."""
    text = json.dumps(obj, default=str, ensure_ascii=False)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


# --------------------------------------------------------------------------- #
# Auth bootstrap — connect a Garmin account from within the app (no laptop needed)
# --------------------------------------------------------------------------- #
@mcp.tool()
def garmin_auth_status() -> str:
    """Report whether the server is connected to a Garmin account (and whose). Call this
    first if other tools report the server isn't authenticated."""
    persistent = _persistent()
    if not _token:
        return _dump(
            {
                "authenticated": False,
                "sign_in_persists": persistent,
                "hint": "Call garmin_auth_start with the account owner's Garmin email "
                "and password to connect.",
            }
        )
    try:
        name = _call("get_full_name")
    except NotAuthenticated as exc:
        return _dump({"authenticated": False, "sign_in_persists": persistent, "hint": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return _dump({"authenticated": True, "warning": f"token present but test call failed: {exc}"})
    return _dump(
        {
            "authenticated": True,
            "account": name,
            "sign_in_persists": persistent,
            **({} if persistent else {
                "warning": "No token volume configured: this sign-in is lost on the next "
                "restart. Attach a Railway volume at /data."
            }),
        }
    )


@mcp.tool()
def garmin_auth_start(email: str, password: str) -> str:
    """Begin connecting a Garmin account, using the account owner's Garmin Connect email
    and password. If Garmin requires a one-time MFA code this returns mfa_required=true —
    then call garmin_auth_complete with the code Garmin sends by email/SMS/app. Accounts
    without MFA connect immediately.

    Privacy note: the password is transmitted in this tool call to the server. Only the
    account owner should use it, on a server they trust."""
    global _pending
    try:
        g = Garmin(email, password, return_on_mfa=True)
        status, _ = g.login()
    except Exception as exc:  # noqa: BLE001
        return _dump({"ok": False, "error": str(exc)})

    if status == "needs_mfa":  # hold this session (its MFA state lives on the instance)
        with _state_lock:
            _pending = {"garmin": g}
        return _dump(
            {
                "ok": True,
                "mfa_required": True,
                "next": "Ask the user for the MFA code Garmin just sent, then call "
                "garmin_auth_complete.",
            }
        )

    return _activate_and_report(g)  # no MFA — already logged in


@mcp.tool()
def garmin_auth_complete(mfa_code: str) -> str:
    """Finish connecting a Garmin account by supplying the MFA one-time code Garmin sent.
    Only needed after garmin_auth_start reported mfa_required."""
    global _pending
    with _state_lock:
        g = _pending.get("garmin")
    if not g:
        return _dump({"ok": False, "error": "No pending login. Call garmin_auth_start first."})
    try:
        # Recent garminconnect keeps the MFA state on the instance and ignores this
        # argument; older versions returned it from login(). Passing {} works for both.
        g.resume_login({}, mfa_code.strip().replace(" ", ""))
    except Exception as exc:  # noqa: BLE001
        # A wrong code leaves the pending session usable for another try.
        return _dump({"ok": False, "error": f"MFA verification failed: {exc}"})
    with _state_lock:
        _pending = {}
    return _activate_and_report(g)


def _activate_and_report(g: Garmin) -> str:
    """Make a freshly logged-in client the active one and save its token, so the
    sign-in survives restarts and the user never has to paste anything."""
    global _client, _token
    # The return_on_mfa login path skips the profile fetch, leaving display_name unset —
    # which breaks endpoints (steps, body battery) that build URLs from it.
    name = None
    try:
        if not getattr(g, "display_name", None):
            g._load_profile_and_settings()
        name = g.get_full_name()
    except Exception:  # noqa: BLE001
        pass
    g.password = None
    token = g.client.dumps()
    _bind_persistence(g)
    with _state_lock:
        _token = token
        _client = g
    # Pull today/yesterday straight away so the first questions answer from the store.
    threading.Thread(target=_safe_sync, name="garmin-post-login-sync", daemon=True).start()

    if _persistent():
        return _dump(
            {
                "ok": True,
                "connected": True,
                "account": name,
                "saved": True,
                "note": "Signed in and saved on the server; it stays connected across "
                "restarts. Nothing to copy.",
            }
        )
    # Without a volume the only way to survive a restart is the env var, so hand the
    # token back — but say plainly that a volume is the real fix.
    return _dump(
        {
            "ok": True,
            "connected": True,
            "account": name,
            "saved": False,
            "token": token,
            "action_required": "No token volume is configured, so this sign-in is lost on "
            "the next restart. Fix: attach a Railway volume mounted at /data (the server "
            "then saves the token itself). Stopgap: paste this token into GARMIN_TOKENS.",
        }
    )


# --------------------------------------------------------------------------- #
# Read tools — biometrics & training
# --------------------------------------------------------------------------- #
@mcp.tool()
def garmin_whoami() -> str:
    """Return the connected Garmin profile (name, id) to verify the connection works."""
    prof = _call("get_full_name")
    uid = _call("get_unit_system") if hasattr(gc(), "get_unit_system") else None
    return _dump({"full_name": prof, "unit_system": uid})


@mcp.tool()
def garmin_daily_summary(cdate: str | None = None) -> str:
    """All-day summary for a date (default today): steps, calories, resting HR, stress,
    body battery, intensity minutes and body-composition snapshot. cdate = 'YYYY-MM-DD'."""
    return _dump(syncer.daily(cdate or _today(), "summary"))


@mcp.tool()
def garmin_sleep(cdate: str | None = None) -> str:
    """Sleep detail for the night ending on cdate (default today): duration, stages
    (deep/light/REM/awake), sleep score, respiration and overnight HRV where available."""
    return _dump(syncer.daily(cdate or _today(), "sleep"))


@mcp.tool()
def garmin_hrv(cdate: str | None = None) -> str:
    """Overnight HRV data for a date (default today): last-night average, weekly average,
    status and the per-reading series. cdate = 'YYYY-MM-DD'."""
    return _dump(syncer.daily(cdate or _today(), "hrv"))


@mcp.tool()
def garmin_training_readiness(cdate: str | None = None) -> str:
    """Training Readiness score and its inputs (sleep, recovery time, HRV, acute load)
    for a date (default today)."""
    return _dump(syncer.daily(cdate or _today(), "readiness"))


@mcp.tool()
def garmin_training_status(cdate: str | None = None) -> str:
    """Training Status for a date (default today): load balance, VO2 max estimate, acute
    and chronic load, and status label (productive/maintaining/detraining/etc.)."""
    return _dump(syncer.daily(cdate or _today(), "training_status"))


@mcp.tool()
def garmin_body_battery(startdate: str, enddate: str | None = None) -> str:
    """Body Battery series between two dates (inclusive). Dates = 'YYYY-MM-DD'.
    Omit enddate for a single day."""
    end = enddate or startdate
    try:
        d0, d1 = date.fromisoformat(startdate), date.fromisoformat(end)
    except ValueError:
        return _dump({"error": "dates must be YYYY-MM-DD"})
    if d1 < d0 or (d1 - d0).days > 62:
        return _dump(_call("get_body_battery", startdate, enddate))
    out: list[Any] = []
    for n in range((d1 - d0).days + 1):
        day = (d0 + timedelta(days=n)).isoformat()
        part = syncer.daily(day, "body_battery")
        out.extend(part if isinstance(part, list) else [part])
    return _dump(out)


@mcp.tool()
def garmin_stress(cdate: str | None = None) -> str:
    """All-day stress breakdown for a date (default today): rest/low/medium/high minutes
    and average stress level."""
    return _dump(syncer.daily(cdate or _today(), "stress"))


@mcp.tool()
def garmin_recent_activities(limit: int = 10, activitytype: str | None = None) -> str:
    """Most recent activities (default 10). Optional activitytype filter, e.g. 'running',
    'strength_training', 'cycling'. Returns summary metrics per activity."""
    last = store.get_meta("last_activities_fetch", 0) or 0
    if time.time() - last < FRESH_MINUTES * 60 and limit <= 30:
        cached = store.recent_activities(limit, activitytype)
        if cached:
            return _dump(cached)
    try:
        items = _call("get_activities", 0, limit, activitytype) or []
    except Exception:
        cached = store.recent_activities(limit, activitytype)
        if cached:
            return _dump(cached)
        raise
    store.put_activities(items)
    if not activitytype:
        store.set_meta("last_activities_fetch", time.time())
    return _dump(items)


@mcp.tool()
def garmin_activity_detail(activity_id: str, include_sets: bool = True) -> str:
    """Full detail for one activity by id, including per-set exercise data for strength
    sessions (reps, weight, exercise category) when include_sets is true."""
    cached = store.get_detail(activity_id)
    if cached and (cached.get("exercise_sets") is not None or not include_sets):
        return _dump(cached)
    out: dict[str, Any] = {"activity": _call("get_activity", activity_id)}
    if include_sets:
        try:
            out["exercise_sets"] = _call("get_activity_exercise_sets", activity_id)
        except Exception as exc:  # noqa: BLE001
            out["exercise_sets_error"] = str(exc)
    if "exercise_sets_error" not in out:
        store.put_detail(activity_id, out)  # finished activities don't change
    return _dump(out)


# --- Summary tools over the local store (fast; mirror the health server's) ---- #
def _fmt_h(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    return f"{int(seconds // 3600)}h {int(round(seconds % 3600 / 60)):02d}m"


def _f(v: Any, digits: int = 0) -> str:
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "—"


def _pretty(v: Any) -> str:
    return v.replace("_", " ").lower() if isinstance(v, str) else "—"


def _ensure_days(days: list[str]) -> None:
    """Fill any of these days that aren't stored yet (uses stored copies where fresh)."""
    for day in days:
        for source in ("summary", "sleep", "hrv", "readiness", "training_status"):
            try:
                syncer.daily(day, source)
            except NotAuthenticated:
                raise
            except Exception:  # noqa: BLE001 — a missing metric shouldn't sink the summary
                pass


@mcp.tool()
def garmin_today() -> str:
    """Today's snapshot: training readiness, last night's sleep (score, stages), overnight
    HRV vs baseline, resting HR, Body Battery, stress, steps, training status/load. Use
    this for 'how am I today / should I train' questions."""
    today = _today()
    _ensure_days([today])
    rows = store.daily_range(today, today)
    if not rows:
        return "No Garmin data for today yet."
    r = rows[0]
    out = [f"# Garmin — {today}", ""]
    out.append(f"**Readiness:** {_f(r['readiness_score'])} ({_pretty(r['readiness_level'])})"
               + (f" · {_pretty(r['readiness_feedback'])}" if r["readiness_feedback"] else ""))
    out.append(f"**Sleep:** {_fmt_h(r['sleep_s'])} · score {_f(r['sleep_score'])} ({_pretty(r['sleep_quality'])}) — "
               f"deep {_fmt_h(r['deep_s'])}, light {_fmt_h(r['light_s'])}, REM {_fmt_h(r['rem_s'])}, awake {_fmt_h(r['awake_s'])}")
    out.append(f"**HRV:** {_f(r['hrv_last_night'])} ms (7-day {_f(r['hrv_weekly'])}, baseline "
               f"{_f(r['hrv_baseline_low'])}–{_f(r['hrv_baseline_high'])}) · {_pretty(r['hrv_status'])}")
    out.append(f"**Resting HR:** {_f(r['resting_hr'])} bpm · **Body Battery:** wake {_f(r['bb_wake'])}, "
               f"high {_f(r['bb_high'])}, low {_f(r['bb_low'])}")
    out.append(f"**Stress avg:** {_f(r['stress_avg'])} · **Steps:** {_f(r['steps'])} · **Active kcal:** {_f(r['active_kcal'])}")
    if r["training_status"] or r["load_acute"] is not None:
        out.append(f"**Training:** {_pretty(r['training_status'])} · acute {_f(r['load_acute'])} / chronic "
                   f"{_f(r['load_chronic'])} (ratio {_f(r['acwr'], 2)})"
                   + (f" · VO2 max {_f(r['vo2max'], 1)}" if r["vo2max"] else ""))
    return "\n".join(out)


@mcp.tool()
def garmin_trends(days: int = 14) -> str:
    """Day-by-day trends with averages: readiness, overnight HRV, resting HR, sleep
    duration and score, Body Battery high, stress, steps. days = 1-365 (default 14).
    Long ranges answer instantly once garmin_sync has backfilled them."""
    days = max(1, min(int(days), 365))
    t = date.fromisoformat(_today())
    span = [(t - timedelta(days=n)).isoformat() for n in range(days)]
    have = {r["date"] for r in store.daily_range(span[-1], span[0])}
    recent = (t - timedelta(days=1)).isoformat()
    # Today/yesterday are kept fresh by the hourly sync; only older gaps need fetching.
    missing = [d for d in span if d not in have or d >= recent]
    gaps = [d for d in missing if d < recent]
    if len(gaps) > 3:
        syncer.start_backfill(days)
        return (f"{len(gaps)} of those days aren't stored yet, so I've started a background "
                f"backfill (~{len(gaps) * 8 // 60 + 1} min). Showing what's stored now; ask again shortly.\n\n"
                + _trends_table(store.daily_range(span[-1], span[0]), days))
    _ensure_days(missing)
    return _trends_table(store.daily_range(span[-1], span[0]), days)


def _trends_table(rows: list[dict[str, Any]], days: int) -> str:
    if not rows:
        return "No stored data for that range yet."
    lines = [f"# Garmin trends — last {days} days", "",
             "| Date | Ready | HRV | RHR | Sleep | Score | BB high | Stress | Steps |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['date']} | {_f(r['readiness_score'])} | {_f(r['hrv_last_night'])} | {_f(r['resting_hr'])} | "
                     f"{_fmt_h(r['sleep_s'])} | {_f(r['sleep_score'])} | {_f(r['bb_high'])} | {_f(r['stress_avg'])} | {_f(r['steps'])} |")

    def avg(k: str) -> float | None:
        v = [r[k] for r in rows if isinstance(r[k], (int, float))]
        return sum(v) / len(v) if v else None

    lines.append("")
    lines.append(f"**Averages:** readiness {_f(avg('readiness_score'))} · HRV {_f(avg('hrv_last_night'))} ms · "
                 f"RHR {_f(avg('resting_hr'))} · sleep {_fmt_h(avg('sleep_s'))} (score {_f(avg('sleep_score'))}) · "
                 f"stress {_f(avg('stress_avg'))} · steps {_f(avg('steps'))}")
    return "\n".join(lines)


@mcp.tool()
def garmin_sync(days: int | None = None) -> str:
    """Refresh stored Garmin data. No days: refresh today/yesterday now and report status.
    days > 2: start a background backfill of that many days (max 730) so trends and older
    dates answer instantly; call again with no days to see progress."""
    if days and days > 2:
        st = syncer.start_backfill(min(int(days), 730))
        return _dump({"backfill": st, "note": "Runs in the background (~8 s per day). Call garmin_sync with no days to check progress."})
    bf = syncer.backfill
    if bf.get("running"):
        return _dump({"backfill": bf, "note": "Backfill running; stored data is usable meanwhile."})
    result = syncer.sync_recent()
    lo, hi = store.stored_range()
    return _dump({"synced": result, "stored_range": [lo, hi], "last_backfill": bf if bf.get("total") else None,
                  "store": "volume" if DB_PATH != ":memory:" else "memory (attach a /data volume to keep it)"})


@mcp.tool()
def garmin_weight_trend(startdate: str, enddate: str | None = None) -> str:
    """Weigh-ins between two dates (inclusive), for tracking body-composition trend.
    Dates = 'YYYY-MM-DD'. Omit enddate to default to today."""
    return _dump(_call("get_weigh_ins", startdate, enddate or _today()))


@mcp.tool()
def garmin_log_weight(weight_kg: float, when: str | None = None) -> str:
    """Log a manual weigh-in to Garmin Connect. weight_kg in kilograms; 'when' optional
    local ISO datetime 'YYYY-MM-DDTHH:MM:SS' (defaults to now). Returns the created record."""
    from datetime import timezone

    tz = None
    if LOCAL_TZ:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(LOCAL_TZ)
        except Exception:  # noqa: BLE001
            tz = None
    try:
        local = datetime.fromisoformat(when) if when else datetime.now(tz)
    except ValueError:
        return _dump({"error": "when must be 'YYYY-MM-DDTHH:MM:SS'"})
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz) if tz else local.astimezone()
    gmt = local.astimezone(timezone.utc)
    # Explicit local + GMT stamps: the library's own conversion uses the server's
    # clock zone (UTC on Railway), which would shift the weigh-in by the UTC offset.
    return _dump(
        _call(
            "add_weigh_in_with_timestamps",
            weight_kg,
            "kg",
            local.replace(tzinfo=None).isoformat(timespec="seconds"),
            gmt.replace(tzinfo=None).isoformat(timespec="seconds"),
        )
    )


# --------------------------------------------------------------------------- #
# Write tools — workouts & scheduling
# --------------------------------------------------------------------------- #
@mcp.tool()
def garmin_list_workouts(limit: int = 25) -> str:
    """List saved workouts in the Garmin workout library (id, name, sport). Use the ids
    with schedule/delete tools."""
    items = _call("get_workouts", 0, limit)
    slim = [
        {
            "workoutId": w.get("workoutId"),
            "name": w.get("workoutName"),
            "sport": (w.get("sportType") or {}).get("sportTypeKey"),
            "updated": w.get("updatedDate"),
        }
        for w in (items or [])
    ]
    return _dump(slim)


@mcp.tool()
def garmin_get_workout(workout_id: str) -> str:
    """Fetch the full JSON of one saved workout by id (useful to copy/adapt its structure)."""
    return _dump(_call("get_workout_by_id", workout_id))


# --- Running workout builder ------------------------------------------------ #
# Converts a compact, structured step spec into Garmin's workout schema. Supports
# distance- or time-based steps, pace / HR-zone / custom-HR / no targets, and nested
# repeat groups (which cover intervals, pyramids and arbitrary varied sessions).

_STEP_KINDS = {
    "warmup": (1, "warmup", 1),
    "cooldown": (2, "cooldown", 2),
    "interval": (3, "interval", 3),
    "recovery": (4, "recovery", 4),
    "rest": (5, "rest", 5),
    "run": (3, "interval", 3),  # alias for a plain work step
}


def _pace_to_mps(pace: str) -> float:
    """'M:SS' per km -> metres/second."""
    mins, secs = pace.strip().split(":")
    total = int(mins) * 60 + int(secs)
    if total <= 0:
        raise ValueError(f"invalid pace '{pace}'")
    return round(1000.0 / total, 6)


def _end_condition(length: dict) -> tuple[dict, float | None]:
    t = (length or {}).get("type", "lap_button")
    if t == "distance":
        metres = float(length["value"]) * (1000.0 if length.get("unit") == "km" else 1.0)
        return {"conditionTypeId": 3, "conditionTypeKey": "distance",
                "displayOrder": 3, "displayable": True}, metres
    if t == "time":
        secs = float(length["value"]) * (60.0 if length.get("unit") == "min" else 1.0)
        return {"conditionTypeId": 2, "conditionTypeKey": "time",
                "displayOrder": 2, "displayable": True}, secs
    return {"conditionTypeId": 1, "conditionTypeKey": "lap.button",
            "displayOrder": 1, "displayable": True}, None


def _target(spec: dict | None) -> tuple[dict, float | None, float | None, int | None]:
    if not spec or spec.get("type") in (None, "none"):
        return ({"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
                 "displayOrder": 1}, None, None, None)
    kind = spec["type"]
    if kind == "pace":  # slow/fast as 'M:SS' per km
        low, high = _pace_to_mps(spec["slow"]), _pace_to_mps(spec["fast"])
        return ({"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone",
                 "displayOrder": 6}, min(low, high), max(low, high), None)
    if kind == "hr_zone":  # zone 1-5
        return ({"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                 "displayOrder": 4}, None, None, int(spec["zone"]))
    if kind == "hr_custom":  # bpm_low/bpm_high
        return ({"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                 "displayOrder": 4}, float(spec["bpm_low"]), float(spec["bpm_high"]), None)
    raise ValueError(f"unknown target type '{kind}'")


def _est_seconds(node: dict) -> float:
    """Rough duration estimate; Garmin recalculates on its side."""
    if node.get("kind") == "repeat":
        inner = sum(_est_seconds(c) for c in node["steps"])
        return inner * int(node["iterations"])
    length = node.get("length", {})
    if length.get("type") == "time":
        return float(length["value"]) * (60.0 if length.get("unit") == "min" else 1.0)
    if length.get("type") == "distance":
        metres = float(length["value"]) * (1000.0 if length.get("unit") == "km" else 1.0)
        tgt = node.get("target") or {}
        mps = _pace_to_mps(tgt["fast"]) if tgt.get("type") == "pace" else 1000.0 / 300.0
        return metres / mps
    return 60.0


def _build_node(node: dict, counter: list[int]):
    from garminconnect.workout import ExecutableStep, RepeatGroup

    counter[0] += 1
    order = counter[0]
    if node.get("kind") == "repeat":
        return RepeatGroup(
            stepOrder=order,
            stepType={"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            numberOfIterations=int(node["iterations"]),
            workoutSteps=[_build_node(c, counter) for c in node["steps"]],
            endCondition={"conditionTypeId": 7, "conditionTypeKey": "iterations",
                          "displayOrder": 7, "displayable": False},
            endConditionValue=float(node["iterations"]),
        )
    kind = node["kind"]
    if kind not in _STEP_KINDS:
        raise ValueError(f"unknown step kind '{kind}'")
    type_id, type_key, disp = _STEP_KINDS[kind]
    end_c, end_v = _end_condition(node.get("length", {}))
    tgt, v1, v2, zone = _target(node.get("target"))
    step = ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": type_id, "stepTypeKey": type_key, "displayOrder": disp},
        endCondition=end_c,
        endConditionValue=end_v,
        targetType=tgt,
    )
    if v1 is not None:
        step.targetValueOne = v1
    if v2 is not None:
        step.targetValueTwo = v2
    if zone is not None:
        step.zoneNumber = zone
    return step


@mcp.tool()
def garmin_create_running_workout(
    name: str, steps_json: str, description: str = ""
) -> str:
    """Create and save a structured RUNNING workout, then return its workoutId.

    steps_json is a JSON array of step nodes (in order). Two node shapes:

    Executable step:
      {"kind": "warmup|interval|recovery|cooldown|rest|run",
       "length": {"type": "distance", "value": 800, "unit": "m"}      # or
                 {"type": "time", "value": 10, "unit": "min"}         # or
                 {"type": "lap_button"},
       "target": {"type": "pace", "slow": "3:45", "fast": "3:35"}     # min:sec per km, or
                 {"type": "hr_zone", "zone": 2}                       # zone 1-5, or
                 {"type": "hr_custom", "bpm_low": 150, "bpm_high": 165}, or
                 {"type": "none"}}

    Repeat group (nest freely for intervals, pyramids, varied sets):
      {"kind": "repeat", "iterations": 6, "steps": [ <nodes> ]}

    Example (10min Z2 warmup, 6x800m @ 3:45-3:35/km w/ 90s jog, 5min cooldown):
      [
        {"kind":"warmup","length":{"type":"time","value":10,"unit":"min"},
         "target":{"type":"hr_zone","zone":2}},
        {"kind":"repeat","iterations":6,"steps":[
          {"kind":"interval","length":{"type":"distance","value":800,"unit":"m"},
           "target":{"type":"pace","slow":"3:45","fast":"3:35"}},
          {"kind":"recovery","length":{"type":"time","value":90,"unit":"s"}}
        ]},
        {"kind":"cooldown","length":{"type":"time","value":5,"unit":"min"}}
      ]

    A pyramid is just a sequence of interval steps with changing lengths (e.g. 400/800/
    1200/800/400), optionally each followed by a recovery step. Distance unit 'm' or 'km';
    time unit 's' or 'min'. Pace targets are min:sec per kilometre."""
    from garminconnect.workout import RunningWorkout, WorkoutSegment

    try:
        spec = json.loads(steps_json)
        if not isinstance(spec, list) or not spec:
            raise ValueError("steps_json must be a non-empty JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        return _dump({"error": f"invalid steps_json: {exc}"})

    try:
        counter = [0]
        steps = [_build_node(node, counter) for node in spec]
        est = int(sum(_est_seconds(node) for node in spec))
    except (KeyError, ValueError) as exc:
        return _dump({"error": f"could not build step: {exc}"})

    running_sport = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
    workout = RunningWorkout(
        workoutName=name,
        description=description or None,
        sportType=running_sport,
        estimatedDurationInSecs=est,
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=running_sport, workoutSteps=steps)
        ],
    )
    result = _call("upload_running_workout", workout)
    return _dump(result)


@mcp.tool()
def garmin_upload_workout_json(workout_json: str) -> str:
    """Escape hatch: create/save a workout from a full Garmin workout JSON payload and
    return its workoutId. Use this only when garmin_create_running_workout can't express
    what you need (e.g. a different sport, or an unusual step attribute). Pass the payload
    as a JSON string matching Garmin's schema (top level: workoutName, sportType,
    workoutSegments[].workoutSteps[] with stepType, endCondition, endConditionValue,
    targetType). If unsure of the exact schema, first call garmin_get_workout on an
    existing workout of the same sport and mirror its structure."""
    try:
        payload = json.loads(workout_json)
    except json.JSONDecodeError as exc:
        return _dump({"error": f"workout_json is not valid JSON: {exc}"})
    return _dump(_call("upload_workout", payload))


@mcp.tool()
def garmin_schedule_workout(workout_id: str, when: str) -> str:
    """Place a saved workout on the Garmin calendar for a date. when = 'YYYY-MM-DD'.
    After scheduling, the user syncs their watch to download it."""
    return _dump(_call("schedule_workout", workout_id, when))


@mcp.tool()
def garmin_scheduled_workouts(year: int, month: int) -> str:
    """List workouts scheduled on the Garmin calendar for a given year and month (1-12)."""
    return _dump(_call("get_scheduled_workouts", year, month))


@mcp.tool()
def garmin_unschedule_workout(scheduled_workout_id: str) -> str:
    """Remove a scheduled workout instance from the calendar by its scheduledWorkoutId
    (does not delete the workout from the library)."""
    return _dump(_call("unschedule_workout", scheduled_workout_id))


@mcp.tool()
def garmin_delete_workout(workout_id: str) -> str:
    """Permanently delete a saved workout from the Garmin library by workoutId."""
    return _dump({"deleted": workout_id, "result": _call("delete_workout", workout_id)})


def _safe_sync() -> None:
    """After sign-in: refresh today/yesterday, then backfill a month in the background
    (stored days are skipped) so trends and recent dates answer instantly."""
    try:
        syncer.sync_recent()
        syncer.start_backfill(30)
    except Exception as exc:  # noqa: BLE001
        print(f"[garmin] sync failed: {str(exc)[:200]}", flush=True)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # Hourly background refresh (and token keep-alive) for as long as the server runs.
    syncer.start_scheduler(lambda: bool(_token))
    # Access logs are suppressed via log_level=WARNING (see FastMCP config above) so the
    # secret-bearing request path is never written to logs.
    print(f"Garmin MCP serving Streamable HTTP at /<secret>/mcp on :{PORT}", flush=True)
    mcp.run(transport="streamable-http")
