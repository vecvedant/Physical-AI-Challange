"""
Predictive maintenance by learning what normal looks like, then noticing when
it stops.

What this is, precisely
-----------------------
Trend and anomaly detection on electrical signatures.  It is *not* motor
current signature analysis.  MCSA resolves sidebands around the supply
frequency to identify broken rotor bars and bearing defects, and that needs
current sampled in the kilohertz.  At 1 Hz those sidebands do not exist in our
data at any resolution, and a system claiming otherwise would be lying.

What we can see instead is that a degrading machine drifts: it draws more real
power for the same work, its power factor sags as the magnetising branch
changes, and its inrush takes longer to decay as it struggles to reach speed.
None of these identify a fault mechanism.  All of them move before a machine
fails, which is enough to send an operator to go and look.

Why this is per-appliance, and why that was not the first design
---------------------------------------------------------------
The obvious approach - fit a density model to the plant's aggregate feature
windows - does not work, and it fails in a way worth recording because it looks
like it is working right up until you test it properly.

Measured: a model trained on three hours of healthy baseline scored *held-out
healthy* data at 0.767 mean anomaly, while genuinely degraded data scored 0.498.
Worse than chance, and inverted.  The reason is that the aggregate window
changes far more when a different mix of machines happens to be running than it
does when one machine degrades.  The model had learned the shift roster, and a
quiet afternoon looked more anomalous than a failing compressor.

So health is scored per appliance, from the switching events NILM has already
attributed to it.  Each start event yields a vector that belongs to exactly one
machine - step size, reactive step, power factor, inrush ratio, settle time -
and is unaffected by whatever else in the plant happens to be running. That is
the whole reason disaggregation has to come before diagnosis.

Two detectors, deliberately
---------------------------
  * A linear autoencoder (PCA reconstruction error).  Catches a sample that
    breaks the *correlation structure* - power factor moving without step size
    moving, say - even when every individual value is in its normal range.
  * An isolation forest.  Catches a sample out of range on some axis even
    though the correlations still hold.

They fail differently, so taking the worse of the two is meaningfully better
than either alone.  Both are pure scikit-learn: torch and tensorflow have no
dependable prebuilt aarch64 story for this board, and a model you cannot
install is worth nothing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CONFIG, MODEL_DIR
from ..pipeline.events import Edge, EdgeType

#: Per-event feature vector.  Every one of these is attributable to a single
#: machine, which is what the plant-wide version could never claim.
EDGE_FEATURES: tuple[str, ...] = (
    "delta_p",        # settled step size, W
    "delta_q",        # settled reactive step, VAr
    "power_factor",   # displacement PF implied by the step
    "inrush_ratio",   # peak over settled step - how hard it starts
    "duration_s",     # how long it took to settle
)


def edge_vector(edge: Edge) -> np.ndarray:
    """Turn a switching event into the vector the health model scores."""
    dp, dq = abs(edge.delta_p), abs(edge.delta_q)
    s = math.hypot(dp, dq)
    pf = dp / s if s > 1e-6 else 1.0
    return np.array([dp, dq, pf, edge.inrush_ratio, edge.duration_s],
                    dtype=np.float64)


#: Where the logistic scoring curve crosses 0.5, in units of the robust
#: healthy scale. Tuned so healthy machines read in the high eighties while a
#: 15% degradation still trips the alert.
ANOMALY_CENTRE: float = 1.3


def _robust_scale(errors: np.ndarray) -> float:
    """
    "As bad as healthy ever gets", estimated so one outlier cannot set it.

    A high percentile is the obvious choice and it is wrong here. The
    calibration slice holds ~20 points, so the 99th percentile of it is just
    the maximum, and a single unusual start inflates the scale enough to flatten
    every score afterwards - measured as a badly degraded machine still reading
    43/100 while a healthy one read 72.

    median + 3 * MAD tracks the same quantity for roughly normal errors, but a
    lone outlier moves the median and the MAD hardly at all.
    """
    errors = np.asarray(errors, dtype=np.float64)
    if errors.size == 0:
        return 1.0
    median = float(np.median(errors))
    mad = float(np.median(np.abs(errors - median)))
    scale = median + 3.0 * 1.4826 * mad
    # A degenerate baseline (every error identical) would give scale == median
    # and divide everything to exactly 1.0, so keep a floor under the spread.
    return max(scale, median * 1.25, 1e-6)


@dataclass
class HealthState:
    """What the monitor currently believes about one machine's condition."""

    subject: str = "plant"
    trained: bool = False
    baseline_samples: int = 0
    baseline_started_t: float = 0.0
    baseline_complete_t: float = 0.0

    #: 0..1, from the most recent event. Higher is worse.
    anomaly_score: float = 0.0
    #: EWMA of the above; what the health score is actually built from.
    smoothed_score: float = 0.0
    #: 0..100, higher is better.
    health_score: float = 100.0

    events_scored: int = 0
    consecutive_anomalies: int = 0
    alert_active: bool = False
    alert_since_t: float = 0.0

    # Slow drift indicators, each a per-day rate.
    pf_drift_per_day: float = 0.0
    power_drift_per_day: float = 0.0
    inrush_drift_per_day: float = 0.0

    last_update_t: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HealthModel:
    """Density model of one machine's normal switching behaviour."""

    def __init__(self, feature_names: tuple[str, ...] = EDGE_FEATURES,
                 min_samples: int = 30) -> None:
        self.feature_names = feature_names
        self.min_samples = min_samples
        self._active = None
        self._scaler = None
        self._pca = None
        self._forest = None
        self._recon_scale = 1.0
        self._forest_scale = 1.0
        self.trained = False

    # ------------------------------------------------------------------ #
    def fit(self, X: np.ndarray) -> bool:
        """
        Fit on baseline vectors.  Returns False if there is too little data.

        Refusing to fit is the right behaviour when data is thin. A model built
        on a handful of samples flags everything, and an operator who gets
        flooded with false alarms in week one switches the alerts off forever.
        """
        if X.ndim != 2 or X.shape[0] < self.min_samples:
            return False

        from sklearn.decomposition import PCA
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler

        # A zero-variance column makes the scaler emit infinities and leaves
        # the PCA basis meaningless, so drop anything that never moves.
        self._active = np.std(X, axis=0) > 1e-9
        if int(self._active.sum()) < 3:
            return False
        Xa = X[:, self._active]

        # Hold out a calibration slice. Scoring thresholds taken from the same
        # data the model was fitted on are optimistic: the model has already
        # seen those points, so their errors are smaller than any future
        # healthy sample's will be, and every honest reading afterwards looks
        # slightly anomalous. Measured here as a healthy machine sitting at
        # 57/100 forever. Calibrating on data the model has not seen closes
        # that gap and is the difference between a health score an operator
        # believes and one they learn to ignore.
        n = Xa.shape[0]
        if n >= 40:
            split = int(n * 0.75)
            X_fit, X_cal = Xa[:split], Xa[split:]
        else:
            X_fit = X_cal = Xa

        self._scaler = StandardScaler().fit(X_fit)
        Z_fit = self._scaler.transform(X_fit)
        Z_cal = self._scaler.transform(X_cal)

        # Strictly fewer components than inputs: a full-rank PCA reconstructs
        # perfectly and its residual carries no information whatsoever.
        n_components = max(1, min(Z_fit.shape[1] - 1,
                                  int(np.ceil(Z_fit.shape[1] * 0.6))))
        self._pca = PCA(n_components=n_components).fit(Z_fit)

        recon = self._pca.inverse_transform(self._pca.transform(Z_cal))
        self._recon_scale = _robust_scale(np.linalg.norm(Z_cal - recon, axis=1))

        self._forest = IsolationForest(
            n_estimators=120,
            contamination=CONFIG.learning.anomaly_contamination,
            random_state=0,
        ).fit(Z_fit)
        self._forest_scale = _robust_scale(-self._forest.score_samples(Z_cal))

        self.trained = True
        return True

    # ------------------------------------------------------------------ #
    def score(self, x: np.ndarray) -> float:
        """Score one vector: 0 is normal, 1 is very unusual."""
        if not self.trained:
            return 0.0
        xa = x[self._active].reshape(1, -1)
        z = self._scaler.transform(xa)

        recon = self._pca.inverse_transform(self._pca.transform(z))
        recon_score = float(np.linalg.norm(z - recon)) / max(self._recon_scale, 1e-9)
        forest_score = float(-self._forest.score_samples(z)[0]) / max(self._forest_scale, 1e-9)

        # A sample only looks healthy if it looks healthy to both detectors.
        combined = max(recon_score, forest_score)
        # Logistic rather than exponential squash. An exponential is too flat
        # near the origin: it mapped ordinary healthy operation to ~0.45, so a
        # perfectly good machine sat at 55/100 forever and told an operator
        # nothing.
        #
        # Centred slightly above the healthy scale rather than on it. At a
        # centre of 1.0 a healthy machine read 67/100, which still looks like a
        # complaint; 1.3 puts routine healthy operation in the high eighties
        # and leaves the alert threshold at roughly 1.4x the healthy spread,
        # which measurement showed still catches 15% wear on the first stage.
        return float(1.0 / (1.0 + math.exp(-3.0 * (combined - ANOMALY_CENTRE))))

    # ------------------------------------------------------------------ #
    def save(self, path: Path) -> None:
        import joblib
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "feature_names": self.feature_names,
            "min_samples": self.min_samples,
            "active": self._active,
            "scaler": self._scaler,
            "pca": self._pca,
            "forest": self._forest,
            "recon_scale": self._recon_scale,
            "forest_scale": self._forest_scale,
        }, path)

    @classmethod
    def load(cls, path: Path) -> "HealthModel":
        import joblib
        blob = joblib.load(path)
        model = cls(tuple(blob["feature_names"]), int(blob.get("min_samples", 30)))
        model._active = blob["active"]
        model._scaler = blob["scaler"]
        model._pca = blob["pca"]
        model._forest = blob["forest"]
        model._recon_scale = blob["recon_scale"]
        model._forest_scale = blob["forest_scale"]
        model.trained = True
        return model


# --------------------------------------------------------------------------- #
class ApplianceHealth:
    """
    Health monitor for one discovered machine.

    Fed only the start events NILM has attributed to this appliance, so nothing
    another machine does can move its score.
    """

    #: Start events needed before the model is fitted.  80 rather than 40 so
    #: that a quarter can be held back for calibration and still be enough
    #: points to take a percentile from. For a machine that cycles a few times
    #: an hour this is about a day of watching; for a rarely-used one it is
    #: correspondingly longer, which is the honest cost of having no labels.
    baseline_events: int = 80

    def __init__(self, subject: str, cfg=None) -> None:
        self.cfg = cfg or CONFIG.learning
        self.state = HealthState(subject=subject)
        self.model = HealthModel(EDGE_FEATURES, min_samples=30)
        self._baseline: list[np.ndarray] = []
        #: (t, pf, delta_p, inrush) for explicit trend lines.
        self._trend: list[tuple[float, float, float, float]] = []

    # ------------------------------------------------------------------ #
    def push_edge(self, edge: Edge) -> HealthState:
        """
        Feed one switching event.

        Only rises are scored. A stop event carries almost no information about
        machine condition - a motor coasting down looks the same worn or new -
        while a start exercises exactly the things that degrade.
        """
        if edge.type is not EdgeType.RISE:
            return self.state

        now = edge.timestamp or time.time()
        self.state.last_update_t = now
        x = edge_vector(edge)
        if not np.all(np.isfinite(x)):
            return self.state

        self._trend.append((now, x[2], x[0], x[3]))
        del self._trend[:-500]

        if not self.model.trained:
            if self.state.baseline_started_t == 0.0:
                self.state.baseline_started_t = now
            self._baseline.append(x)
            self.state.baseline_samples = len(self._baseline)
            if len(self._baseline) >= self.baseline_events:
                if self.model.fit(np.vstack(self._baseline)):
                    self.state.trained = True
                    self.state.baseline_complete_t = now
                    self._baseline.clear()
            return self.state

        score = self.model.score(x)
        self.state.anomaly_score = score
        self.state.events_scored += 1

        alpha = self.cfg.health_ewma_alpha
        # Events are far sparser than samples, so the EWMA needs a faster
        # constant than the 1 Hz stream would want or a machine could fail
        # before the smoothed score noticed.
        alpha = min(0.35, max(alpha, 0.15))
        self.state.smoothed_score = ((1 - alpha) * self.state.smoothed_score
                                     + alpha * score)
        self.state.health_score = round(
            100.0 * (1.0 - min(1.0, self.state.smoothed_score)), 1)

        # A single odd start is a machine doing something unusual, not a
        # machine breaking. Requiring consecutive anomalies is what stops this
        # crying wolf every time someone takes a heavy cut.
        if score > 0.6:
            self.state.consecutive_anomalies += 1
        else:
            self.state.consecutive_anomalies = 0

        was_active = self.state.alert_active
        # Either a run of bad starts, or a smoothed score that has stayed high.
        # Consecutive-count alone flaps: a badly degraded machine that scores
        # 0.55 on the occasional start keeps resetting the counter while its
        # health score sits at 6/100, which is exactly when an operator most
        # needs to be told.
        self.state.alert_active = (
            self.state.consecutive_anomalies >= self.cfg.anomaly_persistence
            or self.state.smoothed_score > 0.75)
        if self.state.alert_active and not was_active:
            self.state.alert_since_t = now
        elif not self.state.alert_active:
            self.state.alert_since_t = 0.0

        self._update_trends()
        return self.state

    # ------------------------------------------------------------------ #
    def _update_trends(self) -> None:
        """
        Explicit drift lines, separate from the anomaly score.

        The density model compares against a frozen baseline, so a degradation
        slow enough could in principle stay inside it and never trip. Trend
        lines catch that, and they are also what an operator can act on:
        "power factor down 0.004 a day" means something to a maintenance
        engineer in a way that "anomaly score 0.42" does not.
        """
        if len(self._trend) < 20:
            return
        arr = np.array(self._trend, dtype=np.float64)
        t_days = (arr[:, 0] - arr[0, 0]) / 86400.0
        if float(np.ptp(t_days)) < 0.25:
            return

        def slope(col: np.ndarray) -> float:
            mask = np.isfinite(col)
            if int(mask.sum()) < 12:
                return 0.0
            x = t_days[mask] - t_days[mask].mean()
            denom = float(np.dot(x, x))
            if denom < 1e-9:
                return 0.0
            return float(np.dot(x, col[mask] - col[mask].mean()) / denom)

        self.state.pf_drift_per_day = slope(arr[:, 1])
        self.state.power_drift_per_day = slope(arr[:, 2])
        self.state.inrush_drift_per_day = slope(arr[:, 3])

    # ------------------------------------------------------------------ #
    def explain(self) -> list[str]:
        """Plain-language reasons for the current state, for the dashboard."""
        s = self.state
        if not s.trained:
            need = self.baseline_events - s.baseline_samples
            return [f"Learning normal behaviour - {s.baseline_samples} starts "
                    f"observed, {max(0, need)} more needed"]

        out: list[str] = []
        if s.alert_active:
            out.append(f"Last {s.consecutive_anomalies} starts were unlike this "
                       f"machine's normal behaviour")
        if s.pf_drift_per_day < -0.002:
            out.append(f"Power factor falling {abs(s.pf_drift_per_day):.4f}/day - "
                       f"check for a degrading motor or a failing capacitor")
        if s.power_drift_per_day > 5.0:
            out.append(f"Drawing {s.power_drift_per_day:.0f} W/day more for the "
                       f"same duty - rising friction or load")
        if s.inrush_drift_per_day > 0.01:
            out.append(f"Start-up inrush growing {s.inrush_drift_per_day:.3f}/day - "
                       f"taking longer to reach speed")
        if not out:
            out.append("Starting normally, within its learned range")
        return out

    # ------------------------------------------------------------------ #
    def save(self, directory: Path | None = None) -> Path:
        directory = Path(directory or MODEL_DIR)
        safe = "".join(c if c.isalnum() else "_" for c in self.state.subject)
        path = directory / f"health_{safe}.joblib"
        self.model.save(path)
        return path

    def load(self, directory: Path | None = None) -> bool:
        directory = Path(directory or MODEL_DIR)
        safe = "".join(c if c.isalnum() else "_" for c in self.state.subject)
        path = directory / f"health_{safe}.joblib"
        if not path.exists():
            return False
        try:
            self.model = HealthModel.load(path)
        except Exception:                                   # noqa: BLE001
            return False
        self.state.trained = True
        return True


# --------------------------------------------------------------------------- #
class HealthRegistry:
    """One :class:`ApplianceHealth` per discovered machine, created on demand."""

    def __init__(self) -> None:
        self._monitors: dict[int, ApplianceHealth] = {}
        self._labels: dict[int, str] = {}

    def for_appliance(self, appliance_id: int, label: str) -> ApplianceHealth:
        self._labels[appliance_id] = label
        if appliance_id not in self._monitors:
            self._monitors[appliance_id] = ApplianceHealth(subject=label)
        else:
            self._monitors[appliance_id].state.subject = label
        return self._monitors[appliance_id]

    def push(self, appliance_id: int, label: str, edge: Edge) -> HealthState:
        return self.for_appliance(appliance_id, label).push_edge(edge)

    def states(self) -> list[dict[str, Any]]:
        out = []
        for aid, mon in self._monitors.items():
            blob = mon.state.to_dict()
            blob["appliance_id"] = aid
            blob["reasons"] = mon.explain()
            out.append(blob)
        return sorted(out, key=lambda b: b.get("health_score", 100.0))

    def alerts(self) -> list[dict[str, Any]]:
        return [s for s in self.states() if s.get("alert_active")]

    def save_all(self, directory: Path | None = None) -> int:
        n = 0
        for mon in self._monitors.values():
            if mon.model.trained:
                mon.save(directory)
                n += 1
        return n
