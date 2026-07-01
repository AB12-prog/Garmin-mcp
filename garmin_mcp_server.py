"""
Garmin Connect MCP server.

A remote MCP server (Streamable HTTP) that exposes your Garmin Connect data and
running-workout authoring as tools Claude can call. Designed to be deployed on Railway
and added to Claude as a custom connector, mirroring a WHOOP-style setup.

Auth model
----------
Garmin has no self-serve individual API, so this server authenticates AS the account
owner using the maintained `garminconnect` library (0.3.x, curl_cffi transport). Provide
a long-lived token blob via the GARMIN_TOKENS env var (mint it once with auth_setup.py).
The token auto-refreshes and lasts ~1 year. If it ever expires you can re-mint locally,
or use the in-app tools (garmin_auth_start / garmin_auth_complete), which return a fresh
blob to paste back into GARMIN_TOKENS. The server keeps no database — it is a stateless
pass-through, and the only persisted secret is that env var.

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
from datetime import date, datetime
from typing import Any

from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PORT = int(os.getenv("PORT", "8000"))
SECRET = os.getenv("GARMIN_MCP_SECRET", "").strip()
# Primary auth: a token blob minted with auth_setup.py (or refreshed via the auth tools).
ENV_TOKENS = os.getenv("GARMIN_TOKENS", "").strip()

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
_token: str = ENV_TOKENS
_pending: dict[str, Any] = {}  # holds Garmin instance + client_state between MFA steps
_state_lock = threading.Lock()  # guards _client / _token / _pending mutations
_call_lock = threading.RLock()  # serializes access to the (non-thread-safe) HTTP client


def _connect() -> Garmin:
    if not _token:
        raise NotAuthenticated(
            "Garmin isn't connected. Ask the account owner for their Garmin email and "
            "password and call garmin_auth_start; if an MFA code is requested, follow up "
            "with garmin_auth_complete."
        )
    client = Garmin()
    client.login(_token)  # login() accepts the token blob directly when > 512 chars
    return client


def gc() -> Garmin:
    global _client
    with _state_lock:
        if _client is None:
            _client = _connect()
        return _client


def _call(fn_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a Garmin client method under a serializing lock, retrying once with a fresh
    login on auth errors. Serialization protects the shared curl_cffi session from
    concurrent tool calls."""
    global _client
    with _call_lock:
        try:
            return getattr(gc(), fn_name)(*args, **kwargs)
        except NotAuthenticated:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(k in msg for k in ("401", "unauthorized", "token", "login", "auth")):
                with _state_lock:
                    _client = None
                return getattr(gc(), fn_name)(*args, **kwargs)
            raise


def _today() -> str:
    return date.today().isoformat()


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
    if not _token:
        return _dump(
            {
                "authenticated": False,
                "hint": "Call garmin_auth_start with the account owner's Garmin email "
                "and password to connect.",
            }
        )
    try:
        name = _call("get_full_name")
    except Exception as exc:  # noqa: BLE001
        return _dump({"authenticated": True, "warning": f"token present but test call failed: {exc}"})
    return _dump(
        {
            "authenticated": True,
            "account": name,
            "persisted": bool(ENV_TOKENS),  # True only if backed by the GARMIN_TOKENS env var
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
        status, client_state = g.login()
    except Exception as exc:  # noqa: BLE001
        return _dump({"ok": False, "error": str(exc)})

    if status:  # MFA required — hold this session for the completion step
        with _state_lock:
            _pending = {"garmin": g, "state": client_state}
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
        pending = dict(_pending)
    g = pending.get("garmin")
    state = pending.get("state")
    if not g or state is None:
        return _dump({"ok": False, "error": "No pending login. Call garmin_auth_start first."})
    try:
        g.resume_login(state, mfa_code.strip())
    except Exception as exc:  # noqa: BLE001
        return _dump({"ok": False, "error": f"MFA verification failed: {exc}"})
    with _state_lock:
        _pending = {}
    return _activate_and_report(g)


def _activate_and_report(g: Garmin) -> str:
    """Make a freshly logged-in client the active one and return the token blob so the
    user can persist it into the GARMIN_TOKENS env var (survives restarts)."""
    global _client, _token
    token = g.client.dumps()
    name = None
    try:
        name = g.get_full_name()
    except Exception:  # noqa: BLE001
        pass
    with _state_lock:
        _token = token
        _client = g
    return _dump(
        {
            "ok": True,
            "connected": True,
            "account": name,
            "token": token,
            "action_required": "To survive server restarts, paste this token as the "
            "GARMIN_TOKENS variable in Railway. Until then it lives in memory only.",
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
    return _dump(_call("get_stats_and_body", cdate or _today()))


@mcp.tool()
def garmin_sleep(cdate: str | None = None) -> str:
    """Sleep detail for the night ending on cdate (default today): duration, stages
    (deep/light/REM/awake), sleep score, respiration and overnight HRV where available."""
    return _dump(_call("get_sleep_data", cdate or _today()))


@mcp.tool()
def garmin_hrv(cdate: str | None = None) -> str:
    """Overnight HRV data for a date (default today): last-night average, weekly average,
    status and the per-reading series. cdate = 'YYYY-MM-DD'."""
    return _dump(_call("get_hrv_data", cdate or _today()))


@mcp.tool()
def garmin_training_readiness(cdate: str | None = None) -> str:
    """Training Readiness score and its inputs (sleep, recovery time, HRV, acute load)
    for a date (default today)."""
    return _dump(_call("get_training_readiness", cdate or _today()))


@mcp.tool()
def garmin_training_status(cdate: str | None = None) -> str:
    """Training Status for a date (default today): load balance, VO2 max estimate, acute
    and chronic load, and status label (productive/maintaining/detraining/etc.)."""
    return _dump(_call("get_training_status", cdate or _today()))


@mcp.tool()
def garmin_body_battery(startdate: str, enddate: str | None = None) -> str:
    """Body Battery series between two dates (inclusive). Dates = 'YYYY-MM-DD'.
    Omit enddate for a single day."""
    return _dump(_call("get_body_battery", startdate, enddate))


@mcp.tool()
def garmin_stress(cdate: str | None = None) -> str:
    """All-day stress breakdown for a date (default today): rest/low/medium/high minutes
    and average stress level."""
    return _dump(_call("get_all_day_stress", cdate or _today()))


@mcp.tool()
def garmin_recent_activities(limit: int = 10, activitytype: str | None = None) -> str:
    """Most recent activities (default 10). Optional activitytype filter, e.g. 'running',
    'strength_training', 'cycling'. Returns summary metrics per activity."""
    return _dump(_call("get_activities", 0, limit, activitytype))


@mcp.tool()
def garmin_activity_detail(activity_id: str, include_sets: bool = True) -> str:
    """Full detail for one activity by id, including per-set exercise data for strength
    sessions (reps, weight, exercise category) when include_sets is true."""
    out: dict[str, Any] = {"activity": _call("get_activity", activity_id)}
    if include_sets:
        try:
            out["exercise_sets"] = _call("get_activity_exercise_sets", activity_id)
        except Exception as exc:  # noqa: BLE001
            out["exercise_sets_error"] = str(exc)
    return _dump(out)


@mcp.tool()
def garmin_weight_trend(startdate: str, enddate: str | None = None) -> str:
    """Weigh-ins between two dates (inclusive), for tracking body-composition trend.
    Dates = 'YYYY-MM-DD'. Omit enddate to default to today."""
    return _dump(_call("get_weigh_ins", startdate, enddate or _today()))


@mcp.tool()
def garmin_log_weight(weight_kg: float, when: str | None = None) -> str:
    """Log a manual weigh-in to Garmin Connect. weight_kg in kilograms; 'when' optional
    ISO datetime 'YYYY-MM-DDTHH:MM:SS' (defaults to now). Returns the created record."""
    ts = when or datetime.now().isoformat()
    return _dump(_call("add_weigh_in", weight_kg, "kg", ts))


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


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # Access logs are suppressed via log_level=WARNING (see FastMCP config above) so the
    # secret-bearing request path is never written to logs.
    print(f"Garmin MCP serving Streamable HTTP at /<secret>/mcp on :{PORT}", flush=True)
    mcp.run(transport="streamable-http")
