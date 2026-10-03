"""
Local data store + background sync for the Garmin MCP server.

Why: every tool call used to go live to Garmin (8-10 s each). Now an hourly background
sync pulls recent days into SQLite on the Railway volume, and tools answer from there in
milliseconds, going live only for data that isn't stored yet or is stale.

Freshness rules
  * Today and yesterday change during the day (steps, readiness, last night's sleep
    settling), so stored copies are used only if fetched within FRESH_MINUTES.
  * Older days are settled: once stored, they're served from the store.
  * If a live fetch fails, a stored copy (however old) is served instead of an error.

Each daily source is stored raw (exactly what Garmin returned, so the existing tools'
output is unchanged) plus a normalised one-row-per-day table for trend tools.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

FRESH_MINUTES = int(os.getenv("GARMIN_FRESH_MINUTES", "60"))
SYNC_INTERVAL_MIN = int(os.getenv("GARMIN_SYNC_INTERVAL_MIN", "60"))
BACKFILL_PACE_S = float(os.getenv("GARMIN_BACKFILL_PACE_S", "1.0"))

# source name -> (Garmin client method, how to call it for a date)
DAILY_SOURCES: dict[str, tuple[str, Callable[[str], tuple]]] = {
    "summary": ("get_stats_and_body", lambda d: (d,)),
    "sleep": ("get_sleep_data", lambda d: (d,)),
    "hrv": ("get_hrv_data", lambda d: (d,)),
    "readiness": ("get_training_readiness", lambda d: (d,)),
    "training_status": ("get_training_status", lambda d: (d,)),
    "stress": ("get_all_day_stress", lambda d: (d,)),
    "body_battery": ("get_body_battery", lambda d: (d, d)),
}


# ------------------------------------------------------------- normalise ---

def _g(obj: Any, *path: Any) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    return cur


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _readiness_pick(entries: Any) -> dict:
    if isinstance(entries, dict):
        return entries
    pool = [e for e in entries or [] if isinstance(e, dict)]
    morning = [e for e in pool if e.get("inputContext") == "AFTER_WAKEUP_RESET"]
    pool = morning or pool
    return max(pool, key=lambda e: str(e.get("timestampLocal") or e.get("timestamp") or "")) if pool else {}


def _training_status_pick(ts: Any) -> dict:
    devices = _g(ts, "mostRecentTrainingStatus", "latestTrainingStatusData")
    if not isinstance(devices, dict):
        return {}
    vals = [v for v in devices.values() if isinstance(v, dict)]
    primary = [v for v in vals if v.get("primaryTrainingDevice")]
    return (primary or vals or [{}])[0]


DAILY_COLUMNS = [
    "steps", "resting_hr", "total_kcal", "active_kcal", "stress_avg", "bb_high", "bb_low", "bb_wake",
    "intensity_moderate_min", "intensity_vigorous_min", "weight_kg",
    "sleep_s", "deep_s", "light_s", "rem_s", "awake_s", "sleep_score", "sleep_quality",
    "hrv_last_night", "hrv_weekly", "hrv_status", "hrv_baseline_low", "hrv_baseline_high",
    "readiness_score", "readiness_level", "readiness_feedback", "recovery_time_min",
    "training_status", "load_acute", "load_chronic", "acwr", "vo2max",
]


def normalise(day: str, raw: dict[str, Any]) -> dict[str, Any]:
    s = _d(raw.get("summary"))
    sl = _d(_g(raw.get("sleep"), "dailySleepDTO"))
    h = _d(_g(raw.get("hrv"), "hrvSummary"))
    r = _readiness_pick(raw.get("readiness"))
    t = _training_status_pick(raw.get("training_status"))
    vo2 = _d(_g(raw.get("training_status"), "mostRecentVO2Max", "generic"))
    weight_g = _num(s.get("weight"))
    return {
        "date": day,
        "steps": _num(s.get("totalSteps")),
        "resting_hr": _num(s.get("restingHeartRate")),
        "total_kcal": _num(s.get("totalKilocalories")),
        "active_kcal": _num(s.get("activeKilocalories")),
        "stress_avg": _num(s.get("averageStressLevel")),
        "bb_high": _num(s.get("bodyBatteryHighestValue")),
        "bb_low": _num(s.get("bodyBatteryLowestValue")),
        "bb_wake": _num(s.get("bodyBatteryAtWakeTime")),
        "intensity_moderate_min": _num(s.get("moderateIntensityMinutes")),
        "intensity_vigorous_min": _num(s.get("vigorousIntensityMinutes")),
        "weight_kg": round(weight_g / 1000, 2) if weight_g else None,
        "sleep_s": _num(sl.get("sleepTimeSeconds")),
        "deep_s": _num(sl.get("deepSleepSeconds")),
        "light_s": _num(sl.get("lightSleepSeconds")),
        "rem_s": _num(sl.get("remSleepSeconds")),
        "awake_s": _num(sl.get("awakeSleepSeconds")),
        "sleep_score": _num(_g(sl, "sleepScores", "overall", "value")),
        "sleep_quality": _g(sl, "sleepScores", "overall", "qualifierKey"),
        "hrv_last_night": _num(h.get("lastNightAvg")),
        "hrv_weekly": _num(h.get("weeklyAvg")),
        "hrv_status": h.get("status"),
        "hrv_baseline_low": _num(_g(h, "baseline", "balancedLow")),
        "hrv_baseline_high": _num(_g(h, "baseline", "balancedUpper")),
        "readiness_score": _num(r.get("score")),
        "readiness_level": r.get("level"),
        "readiness_feedback": r.get("feedbackShort"),
        "recovery_time_min": _num(r.get("recoveryTime")),
        "training_status": t.get("trainingStatusFeedbackPhrase"),
        "load_acute": _num(_g(t, "acuteTrainingLoadDTO", "dailyTrainingLoadAcute")),
        "load_chronic": _num(_g(t, "acuteTrainingLoadDTO", "dailyTrainingLoadChronic")),
        "acwr": _num(_g(t, "acuteTrainingLoadDTO", "dailyAcuteChronicWorkloadRatio")),
        "vo2max": _num(vo2.get("vo2MaxPreciseValue")) or _num(vo2.get("vo2MaxValue")),
    }


# ----------------------------------------------------------------- store ---

class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS daily_raw (
                    date TEXT NOT NULL, source TEXT NOT NULL, json TEXT,
                    fetched_at REAL NOT NULL, PRIMARY KEY (date, source));
                CREATE TABLE IF NOT EXISTS daily (
                    date TEXT PRIMARY KEY, {", ".join(f"{c}" for c in DAILY_COLUMNS)}, updated_at REAL);
                CREATE TABLE IF NOT EXISTS activities (
                    activity_id INTEGER PRIMARY KEY, start_local TEXT, type_key TEXT,
                    json TEXT, fetched_at REAL);
                CREATE INDEX IF NOT EXISTS idx_act_start ON activities(start_local);
                CREATE TABLE IF NOT EXISTS activity_detail (
                    activity_id INTEGER PRIMARY KEY, json TEXT, fetched_at REAL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )

    # ---- raw daily
    def get_raw(self, day: str, source: str) -> tuple[Any, float] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT json, fetched_at FROM daily_raw WHERE date=? AND source=?", (day, source)
            ).fetchone()
        if not row:
            return None
        return (json.loads(row[0]) if row[0] is not None else None), row[1]

    def put_raw(self, day: str, source: str, data: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO daily_raw (date, source, json, fetched_at) VALUES (?,?,?,?)",
                (day, source, json.dumps(data, default=str), time.time()),
            )
        self._renormalise(day)

    def _renormalise(self, day: str) -> None:
        with self._lock:
            rows = self._db.execute("SELECT source, json FROM daily_raw WHERE date=?", (day,)).fetchall()
            raw = {src: (json.loads(js) if js else None) for src, js in rows}
            norm = normalise(day, raw)
            cols = ["date", *DAILY_COLUMNS, "updated_at"]
            vals = [norm.get(c) for c in ["date", *DAILY_COLUMNS]] + [time.time()]
            self._db.execute(
                f"INSERT OR REPLACE INTO daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            )

    def daily_range(self, start: str, end: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM daily WHERE date BETWEEN ? AND ? ORDER BY date DESC", (start, end))
            names = [c[0] for c in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    # ---- activities
    def put_activities(self, items: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._lock:
            for a in items:
                if not isinstance(a, dict) or a.get("activityId") is None:
                    continue
                self._db.execute(
                    "INSERT OR REPLACE INTO activities (activity_id, start_local, type_key, json, fetched_at) VALUES (?,?,?,?,?)",
                    (a["activityId"], a.get("startTimeLocal"), _g(a, "activityType", "typeKey"), json.dumps(a, default=str), now),
                )

    def recent_activities(self, limit: int, activitytype: str | None) -> list[dict[str, Any]]:
        q = "SELECT json FROM activities"
        args: list[Any] = []
        if activitytype:
            q += " WHERE type_key = ?"
            args.append(activitytype)
        q += " ORDER BY start_local DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [json.loads(r[0]) for r in self._db.execute(q, args).fetchall()]

    def get_detail(self, activity_id: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT json FROM activity_detail WHERE activity_id=?", (int(activity_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def put_detail(self, activity_id: str, data: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO activity_detail (activity_id, json, fetched_at) VALUES (?,?,?)",
                (int(activity_id), json.dumps(data, default=str), time.time()),
            )

    # ---- meta
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, json.dumps(value, default=str)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def stored_range(self) -> tuple[str | None, str | None]:
        with self._lock:
            return self._db.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()


# ------------------------------------------------------------------ sync ---

class Syncer:
    """Fetch-through cache plus the hourly background sync and backfill."""

    def __init__(self, store: Store, call: Callable[..., Any], today: Callable[[], str]):
        self.store = store
        self.call = call          # the server's serialised, auth-retrying Garmin caller
        self.today = today        # owner's local date
        self.backfill: dict[str, Any] = {"running": False}
        self._sync_lock = threading.Lock()

    def _recent(self, day: str) -> bool:
        t = date.fromisoformat(self.today())
        return day >= (t - timedelta(days=1)).isoformat()

    def daily(self, day: str, source: str, *, force: bool = False) -> Any:
        """Stored copy if usable, else live fetch (stored on success). Falls back to a
        stored copy of any age if Garmin can't be reached."""
        cached = self.store.get_raw(day, source)
        if cached and not force:
            data, fetched_at = cached
            if not self._recent(day) or time.time() - fetched_at < FRESH_MINUTES * 60:
                return data
        method, argfn = DAILY_SOURCES[source]
        try:
            data = self.call(method, *argfn(day))
        except Exception:
            if cached:
                return cached[0]
            raise
        self.store.put_raw(day, source, data)
        return data

    def sync_days(self, days: list[str], *, force: bool, pace_s: float = 0.0, progress: Callable[[], None] | None = None) -> None:
        for i, day in enumerate(days):
            for source in DAILY_SOURCES:
                if pace_s and i:
                    time.sleep(pace_s / len(DAILY_SOURCES))
                try:
                    self.daily(day, source, force=force)
                except Exception as exc:  # noqa: BLE001 — one missing metric shouldn't stop the day
                    msg = str(exc).lower()
                    if any(k in msg for k in ("401", "unauthorized", "authentication", "expired", "429", "too many")):
                        raise
            if progress:
                progress()

    def sync_recent(self) -> dict[str, Any]:
        """Refresh today + yesterday and recent activities. Runs hourly in the background."""
        if not self._sync_lock.acquire(blocking=False):
            return {"skipped": "sync already running"}
        try:
            t = date.fromisoformat(self.today())
            days = [(t - timedelta(days=1)).isoformat(), t.isoformat()]
            self.sync_days(days, force=True)
            acts = self.call("get_activities", 0, 30) or []
            self.store.put_activities(acts)
            self.store.set_meta("last_activities_fetch", time.time())
            self.store.set_meta("last_sync", {"at": datetime.now().isoformat(timespec="seconds"), "ok": True})
            return {"ok": True, "days": days, "activities": len(acts)}
        except Exception as exc:  # noqa: BLE001
            self.store.set_meta("last_sync", {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "error": str(exc)[:300]})
            raise
        finally:
            self._sync_lock.release()

    def start_backfill(self, days: int) -> dict[str, Any]:
        if self.backfill.get("running"):
            return dict(self.backfill)
        t = date.fromisoformat(self.today())
        span = [(t - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]
        self.backfill = {"running": True, "total": days, "done": 0, "from": span[0], "to": span[-1]}

        def bump() -> None:
            self.backfill["done"] += 1

        def run() -> None:
            try:
                with self._sync_lock:
                    # force=False: days already stored and settled are skipped, so a
                    # repeat backfill only fetches what's missing.
                    self.sync_days(span, force=False, pace_s=BACKFILL_PACE_S, progress=bump)
                    acts = self.call("get_activities", 0, 200) or []
                    self.store.put_activities(acts)
                    self.store.set_meta("last_activities_fetch", time.time())
                self.backfill.update(running=False, finished=datetime.now().isoformat(timespec="seconds"))
            except Exception as exc:  # noqa: BLE001
                self.backfill.update(running=False, error=str(exc)[:300])

        threading.Thread(target=run, name="garmin-backfill", daemon=True).start()
        return dict(self.backfill)

    def start_scheduler(self, is_connected: Callable[[], bool]) -> None:
        """Hourly background refresh inside the server process (the service runs
        continuously, so no separate Railway cron service is needed). Also keeps the
        Garmin token chain refreshed even when nobody uses Claude for a while."""

        def loop() -> None:
            time.sleep(20)  # let the server finish booting
            while True:
                if is_connected() and not self.backfill.get("running"):
                    try:
                        self.sync_recent()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[garmin] scheduled sync failed: {str(exc)[:200]}", flush=True)
                time.sleep(SYNC_INTERVAL_MIN * 60)

        threading.Thread(target=loop, name="garmin-scheduler", daemon=True).start()
