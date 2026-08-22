"""
Multi-horizon load forecasting, self-supervised on the site's own history.

The dispatch optimiser needs to know what the plant will draw over the next 24
hours before it can decide when to charge a battery.  Nobody is going to label
that, and they do not need to: the target is simply the plant's own demand a
few blocks later, which the historian already recorded.  Every training pair is
manufactured from the past by construction.

Direct multi-horizon, not recursive
-----------------------------------
One model per horizon, each predicting that horizon directly from features
known now.  The recursive alternative - predict one step, feed it back, repeat -
is cheaper to train but compounds its own error, and by the 96th step of a
24-hour plan it is predicting mostly from its own mistakes.  Since dispatch
weighs the far end of the horizon against the near end, that bias would land
straight in the battery schedule.

HistGradientBoostingRegressor, not XGBoost
------------------------------------------
Same algorithm family, ships inside scikit-learn, and installs on the board's
aarch64 Debian without a compiler.  XGBoost would mean building from source on
a 2 GHz A53, and a dependency that fails to install on the target is not a
dependency, it is a bug.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import CONFIG, MODEL_DIR

#: Lags, in blocks.  Covers the last two hours in detail, then yesterday and
#: the day before at the same time of day, which is where most of the signal in
#: an industrial load profile actually lives.
LAG_BLOCKS: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 96, 97, 192)

#: Rolling-window means, in blocks.
ROLL_BLOCKS: tuple[int, ...] = (4, 12, 96)


@dataclass
class ForecastPoint:
    """One predicted block."""

    timestamp: float
    horizon_blocks: int
    predicted_w: float
    #: Empirical prediction interval from validation residuals, not a
    #: distributional assumption.
    lower_w: float = 0.0
    upper_w: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "horizon_blocks": self.horizon_blocks,
            "predicted_w": round(self.predicted_w, 1),
            "lower_w": round(self.lower_w, 1),
            "upper_w": round(self.upper_w, 1),
        }


def blockify(timestamps: np.ndarray, values: np.ndarray,
             block_minutes: int = 15) -> tuple[np.ndarray, np.ndarray]:
    """
    Average a 1 Hz series into fixed wall-clock blocks.

    Aligned to absolute time rather than to the first sample, so that blocks
    from different runs line up and yesterday's 14:00 is comparable with
    today's.
    """
    if timestamps.size == 0:
        return np.zeros(0), np.zeros(0)
    span = block_minutes * 60
    keys = (timestamps // span).astype(np.int64)
    uniq, inverse = np.unique(keys, return_inverse=True)
    sums = np.bincount(inverse, weights=values, minlength=uniq.size)
    counts = np.bincount(inverse, minlength=uniq.size)
    return uniq * span, sums / np.maximum(counts, 1)


def _build_features(block_t: np.ndarray, block_v: np.ndarray,
                    idx: np.ndarray) -> np.ndarray:
    """Feature matrix for the given block indices."""
    rows = []
    for i in idx:
        row: list[float] = []
        for lag in LAG_BLOCKS:
            row.append(block_v[i - lag] if i - lag >= 0 else block_v[0])
        for win in ROLL_BLOCKS:
            lo = max(0, i - win + 1)
            row.append(float(np.mean(block_v[lo:i + 1])))
            row.append(float(np.std(block_v[lo:i + 1])) if i - lo >= 1 else 0.0)
        lt = time.localtime(block_t[i])
        hour = lt.tm_hour + lt.tm_min / 60.0
        row += [
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0),
            math.sin(2 * math.pi * lt.tm_wday / 7.0),
            math.cos(2 * math.pi * lt.tm_wday / 7.0),
            1.0 if lt.tm_wday >= 5 else 0.0,
        ]
        rows.append(row)
    return np.asarray(rows, dtype=np.float64)


class LoadForecaster:
    """One gradient-boosting model per horizon."""

    def __init__(self, horizons: tuple[int, ...] | None = None,
                 block_minutes: int | None = None) -> None:
        cfg = CONFIG.learning
        self.horizons = tuple(horizons or cfg.forecast_horizons)
        self.block_minutes = block_minutes or cfg.forecast_block_minutes
        self._models: dict[int, Any] = {}
        #: Validation residual spread per horizon, for the prediction interval.
        self._residual: dict[int, float] = {}
        self.trained = False
        self.trained_at = 0.0
        self.n_train = 0

    # ------------------------------------------------------------------ #
    @property
    def min_history(self) -> int:
        """Blocks needed before training is worth attempting."""
        return max(CONFIG.learning.min_history_blocks, max(self.horizons) + max(LAG_BLOCKS) + 8)

    def fit(self, timestamps: np.ndarray, power_w: np.ndarray) -> bool:
        """
        Train from a raw 1 Hz history.  Returns False if there is not enough.

        Splits chronologically, never randomly: a shuffled split lets the model
        see the future of the very block it is scored on, and the resulting
        validation number would be fiction.
        """
        block_t, block_v = blockify(timestamps, power_w, self.block_minutes)
        if block_v.size < self.min_history:
            return False

        from sklearn.ensemble import HistGradientBoostingRegressor

        start = max(LAG_BLOCKS)
        ok = False
        for h in self.horizons:
            idx = np.arange(start, block_v.size - h)
            if idx.size < 40:
                continue
            X = _build_features(block_t, block_v, idx)
            y = block_v[idx + h]

            split = int(idx.size * 0.8)
            model = HistGradientBoostingRegressor(
                max_iter=200,
                learning_rate=0.06,
                max_depth=6,
                min_samples_leaf=10,
                l2_regularization=1.0,
                random_state=0,
            )
            model.fit(X[:split], y[:split])

            if idx.size - split >= 5:
                pred = model.predict(X[split:])
                resid = y[split:] - pred
                # Half the central 80% interval: an empirical spread rather
                # than an assumption that residuals are Gaussian, which for a
                # load that switches in steps they emphatically are not.
                self._residual[h] = float(
                    (np.percentile(resid, 90) - np.percentile(resid, 10)) / 2.0)
            else:
                self._residual[h] = float(np.std(y)) if y.size else 0.0

            # Refit on everything now that the interval is measured; throwing
            # away the most recent fifth of history would be perverse when it
            # is the most relevant part.
            model.fit(X, y)
            self._models[h] = model
            ok = True

        if ok:
            self.trained = True
            self.trained_at = time.time()
            self.n_train = int(block_v.size)
        return ok

    # ------------------------------------------------------------------ #
    def predict(self, timestamps: np.ndarray,
                power_w: np.ndarray) -> list[ForecastPoint]:
        """Forecast every configured horizon from the current history tail."""
        if not self.trained:
            return []
        block_t, block_v = blockify(timestamps, power_w, self.block_minutes)
        if block_v.size < max(LAG_BLOCKS) + 1:
            return []

        i = block_v.size - 1
        X = _build_features(block_t, block_v, np.array([i]))
        span = self.block_minutes * 60

        out: list[ForecastPoint] = []
        for h in sorted(self._models):
            value = float(self._models[h].predict(X)[0])
            value = max(0.0, value)
            spread = abs(self._residual.get(h, 0.0))
            out.append(ForecastPoint(
                timestamp=float(block_t[i]) + h * span,
                horizon_blocks=h,
                predicted_w=value,
                lower_w=max(0.0, value - spread),
                upper_w=value + spread,
            ))
        return out

    def profile(self, timestamps: np.ndarray, power_w: np.ndarray,
                blocks: int) -> np.ndarray:
        """
        A dense per-block profile for the dispatch optimiser.

        Dispatch needs a value for every one of the next N blocks, but training
        a model per block would be 96 models for a day. We train a handful of
        anchor horizons and interpolate between them, which costs a little
        accuracy in the gaps and saves an enormous amount of compute on a board
        that has other work to do.
        """
        points = self.predict(timestamps, power_w)
        if not points:
            return np.zeros(blocks)
        xs = np.array([p.horizon_blocks for p in points], dtype=np.float64)
        ys = np.array([p.predicted_w for p in points], dtype=np.float64)
        grid = np.arange(1, blocks + 1, dtype=np.float64)
        return np.interp(grid, xs, ys, left=ys[0], right=ys[-1])

    # ------------------------------------------------------------------ #
    def save(self, path: Path | None = None) -> Path:
        import joblib
        path = Path(path or (MODEL_DIR / "load_forecast.joblib"))
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "models": self._models,
            "residual": self._residual,
            "horizons": self.horizons,
            "block_minutes": self.block_minutes,
            "trained_at": self.trained_at,
            "n_train": self.n_train,
        }, path)
        return path

    def load(self, path: Path | None = None) -> bool:
        import joblib
        path = Path(path or (MODEL_DIR / "load_forecast.joblib"))
        if not path.exists():
            return False
        try:
            blob = joblib.load(path)
        except Exception:                                   # noqa: BLE001
            return False
        self._models = blob["models"]
        self._residual = blob["residual"]
        self.horizons = tuple(blob["horizons"])
        self.block_minutes = int(blob["block_minutes"])
        self.trained_at = float(blob.get("trained_at", 0.0))
        self.n_train = int(blob.get("n_train", 0))
        self.trained = bool(self._models)
        return self.trained


# --------------------------------------------------------------------------- #
def evaluate(forecaster: LoadForecaster, timestamps: np.ndarray,
             power_w: np.ndarray) -> dict[str, float]:
    """
    Walk-forward evaluation on data the model has not seen.

    Reported as MAPE against the mean rather than per-point: an industrial load
    spends the night near zero, and dividing by a near-zero actual produces
    percentage errors in the thousands that say nothing about the model.
    """
    block_t, block_v = blockify(timestamps, power_w, forecaster.block_minutes)
    start = max(LAG_BLOCKS)
    results: dict[str, float] = {}
    mean_load = float(np.mean(block_v)) if block_v.size else 1.0

    for h, model in sorted(forecaster._models.items()):
        idx = np.arange(start, block_v.size - h)
        if idx.size < 10:
            continue
        X = _build_features(block_t, block_v, idx)
        y = block_v[idx + h]
        pred = np.maximum(0.0, model.predict(X))
        mae = float(np.mean(np.abs(y - pred)))
        results[f"h{h}_mae_w"] = round(mae, 1)
        results[f"h{h}_nmae_pct"] = round(100.0 * mae / max(mean_load, 1.0), 1)
    return results
