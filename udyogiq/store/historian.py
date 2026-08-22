"""
On-board historian.

SQLite because the whole point of this project is that nothing leaves the site.
A cloud time-series database would be easier and would also make the device
useless in exactly the situation it was built for - a factory with no reliable
internet and an owner who does not want their production data on someone
else's server.

Storage discipline
------------------
The board has finite flash and is expected to run for years, so raw samples are
kept only for a couple of days and then rolled into one-minute aggregates.
That is not a compromise: nobody looks at second-resolution power from three
weeks ago, but everybody wants a year of daily energy, and the aggregate is
about sixty times smaller.

Writes are batched.  Committing every sample at 1 Hz would put a synchronous
fsync in the acquisition path, and on eMMC that is both slow and a good way to
wear the flash out early.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import CONFIG
from ..meter.frame import MeterFrame

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    t              REAL PRIMARY KEY,
    active_w       REAL, reactive_var REAL, apparent_va REAL,
    voltage_v      REAL, current_a    REAL, power_factor REAL,
    frequency_hz   REAL,
    solar_w        REAL, battery_w    REAL, soc          REAL,
    source         TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_t ON samples(t);

CREATE TABLE IF NOT EXISTS minute_agg (
    t              INTEGER PRIMARY KEY,   -- unix minute
    active_w_mean  REAL, active_w_max REAL, active_w_min REAL,
    reactive_mean  REAL, voltage_mean REAL, pf_mean      REAL,
    solar_w_mean   REAL, battery_w_mean REAL, soc_mean   REAL,
    import_kwh     REAL, export_kwh   REAL,
    n              INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    t              REAL, type TEXT,
    delta_p        REAL, delta_q REAL,
    pre_level_p    REAL, post_level_p REAL,
    peak_p         REAL, duration_s   REAL,
    appliance_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_t ON events(t);

CREATE TABLE IF NOT EXISTS appliances (
    id             INTEGER PRIMARY KEY,
    label          TEXT, kind TEXT,
    mean_dp        REAL, mean_dq REAL, mean_inrush REAL,
    observations   INTEGER, cycles INTEGER,
    total_runtime_s REAL, total_energy_wh REAL,
    health_score   REAL,
    user_named     INTEGER,
    first_seen_t   REAL, last_seen_t REAL,
    updated_t      REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    t              REAL, kind TEXT, outcome TEXT,
    target         TEXT, target_id INTEGER,
    reason         TEXT, value_inr_per_h REAL,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_t ON decisions(t);

CREATE TABLE IF NOT EXISTS daily (
    day            TEXT PRIMARY KEY,      -- YYYY-MM-DD
    import_kwh     REAL, export_kwh REAL, solar_kwh REAL,
    load_kwh       REAL, idle_kwh   REAL,
    cost_inr       REAL, saving_inr REAL, co2_kg REAL,
    peak_kva       REAL,
    updated_t      REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key            TEXT PRIMARY KEY,
    value          TEXT
);
"""


class Historian:
    """Batched SQLite writer with automatic rollup and retention."""

    def __init__(self, path: str | Path | None = None,
                 batch_size: int = 60) -> None:
        self.path = Path(path or CONFIG.store.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size

        # check_same_thread=False because acquisition and the API server run on
        # different threads; every access is guarded by the lock below.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        # WAL lets the dashboard read while acquisition writes, which is the
        # whole access pattern here.
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL rather than FULL: losing the last second of samples in a power
        # cut is survivable, and a synchronous fsync per commit is not, on eMMC
        # that has to last years.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.commit()

        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._last_rollup = 0.0
        self._last_retention = 0.0

    # ------------------------------------------------------------------ #
    def add_sample(self, frame: MeterFrame, *, solar_w: float = 0.0,
                   battery_w: float = 0.0, soc: float = 0.0) -> None:
        """Queue one sample. Flushed when the batch fills."""
        if not frame.valid:
            return
        self._pending.append((
            frame.timestamp, frame.active_power_w, frame.reactive_power_var,
            frame.apparent_power_va, frame.voltage_v, frame.current_a,
            frame.power_factor, frame.frequency_hz,
            solar_w, battery_w, soc, frame.source,
        ))
        if len(self._pending) >= self.batch_size:
            self.flush()

    def flush(self) -> int:
        if not self._pending:
            return 0
        rows, self._pending = self._pending, []
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------------ #
    def add_event(self, edge: Any, appliance_id: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (t,type,delta_p,delta_q,pre_level_p,"
                "post_level_p,peak_p,duration_s,appliance_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (edge.timestamp, edge.type.value, edge.delta_p, edge.delta_q,
                 edge.pre_level_p, edge.post_level_p, edge.peak_p,
                 edge.duration_s, appliance_id))
            self._conn.commit()

    def upsert_appliance(self, app: Any, health_score: float = 100.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO appliances (id,label,kind,mean_dp,mean_dq,"
                "mean_inrush,observations,cycles,total_runtime_s,total_energy_wh,"
                "health_score,user_named,first_seen_t,last_seen_t,updated_t) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (app.id, app.label, app.kind, app.mean_dp, app.mean_dq,
                 app.mean_inrush, app.observations, app.cycles,
                 app.total_runtime_s, app.total_energy_wh, health_score,
                 int(app.user_named), app.first_seen_t, app.last_seen_t,
                 time.time()))
            self._conn.commit()

    def add_decision(self, decision: Any) -> None:
        d = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions (t,kind,outcome,target,target_id,reason,"
                "value_inr_per_h,detail) VALUES (?,?,?,?,?,?,?,?)",
                (d.get("timestamp"), d.get("kind"), d.get("outcome"),
                 d.get("target"), d.get("target_id"), d.get("reason"),
                 d.get("value_inr_per_h", 0.0),
                 json.dumps(d.get("detail", {}))))
            self._conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                               (key, json.dumps(value)))
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ------------------------------------------------------------------ #
    def rollup(self, now: float | None = None, force: bool = False) -> int:
        """
        Fold raw samples into one-minute aggregates.

        Only complete minutes are rolled up; the current minute is still
        accumulating and aggregating it would produce a value that changes
        after the fact.
        """
        now = now if now is not None else time.time()
        if not force and now - self._last_rollup < 60.0:
            return 0
        self._last_rollup = now
        self.flush()

        current_minute = int(now // 60)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR REPLACE INTO minute_agg "
                "SELECT CAST(t/60 AS INTEGER) AS m, "
                "  AVG(active_w), MAX(active_w), MIN(active_w), "
                "  AVG(reactive_var), AVG(voltage_v), AVG(power_factor), "
                "  AVG(solar_w), AVG(battery_w), AVG(soc), "
                "  SUM(MAX(active_w,0))/60.0/1000.0, "
                "  SUM(MAX(-active_w,0))/60.0/1000.0, "
                "  COUNT(*) "
                "FROM samples WHERE CAST(t/60 AS INTEGER) < ? "
                "GROUP BY m", (current_minute,))
            self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def enforce_retention(self, now: float | None = None,
                          force: bool = False) -> int:
        """Drop raw samples older than the retention window."""
        now = now if now is not None else time.time()
        if not force and now - self._last_retention < 3600.0:
            return 0
        self._last_retention = now

        self.rollup(now, force=True)
        raw_cutoff = now - CONFIG.store.raw_retention_hours * 3600
        agg_cutoff = int((now - CONFIG.store.aggregate_retention_days * 86400) // 60)
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM samples WHERE t < ?", (raw_cutoff,)).rowcount
            self._conn.execute("DELETE FROM minute_agg WHERE t < ?", (agg_cutoff,))
            self._conn.execute("DELETE FROM events WHERE t < ?", (agg_cutoff * 60,))
            self._conn.commit()
        return max(0, deleted)

    # ------------------------------------------------------------------ #
    def recent_samples(self, seconds: float = 3600,
                       now: float | None = None) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                "SELECT t,active_w,reactive_var,voltage_v,power_factor,"
                "solar_w,battery_w,soc FROM samples WHERE t >= ? ORDER BY t",
                (now - seconds,)).fetchall()
        keys = ("t", "active_w", "reactive_var", "voltage_v", "power_factor",
                "solar_w", "battery_w", "soc")
        return [dict(zip(keys, r)) for r in rows]

    def minute_series(self, hours: float = 24,
                      now: float | None = None) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        self.rollup(now)
        start = int((now - hours * 3600) // 60)
        with self._lock:
            rows = self._conn.execute(
                "SELECT t,active_w_mean,active_w_max,solar_w_mean,battery_w_mean,"
                "soc_mean FROM minute_agg WHERE t >= ? ORDER BY t",
                (start,)).fetchall()
        keys = ("minute", "active_w", "active_w_max", "solar_w", "battery_w", "soc")
        return [dict(zip(keys, r)) for r in rows]

    def training_series(self, days: float = 14,
                        now: float | None = None) -> tuple[list[float], list[float]]:
        """
        Timestamps and active power for model training.

        Reads from the minute aggregates rather than raw samples, because the
        forecaster blocks everything into quarter hours anyway and raw
        resolution is deleted after a couple of days.
        """
        now = now if now is not None else time.time()
        self.rollup(now)
        start = int((now - days * 86400) // 60)
        with self._lock:
            rows = self._conn.execute(
                "SELECT t, active_w_mean FROM minute_agg "
                "WHERE t >= ? AND active_w_mean IS NOT NULL ORDER BY t",
                (start,)).fetchall()
        return [r[0] * 60.0 for r in rows], [r[1] for r in rows]

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t,kind,outcome,target,reason,value_inr_per_h "
                "FROM decisions ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
        keys = ("t", "kind", "outcome", "target", "reason", "value_inr_per_h")
        return [dict(zip(keys, r)) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            def count(table: str) -> int:
                return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            out = {t: count(t) for t in
                   ("samples", "minute_agg", "events", "appliances", "decisions")}
        out["db_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        out["pending_writes"] = len(self._pending)
        return out

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._conn.close()
