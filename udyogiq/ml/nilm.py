"""
Non-Intrusive Load Monitoring: recovering individual machines from one meter.

The premise is that a plant's aggregate power is a sum of a small number of
loads that switch in discrete steps, and that each load leaves a repeatable
signature when it does.  If we can group the switching events by signature, we
have recovered the machines without ever metering them individually.

Why this is clustering and not classification
---------------------------------------------
There is no labelled data and there never will be.  Nobody is going to
instrument a workshop to tell us which of its machines produced which step, and
a model trained on someone else's factory would not transfer - the whole point
is that these are *this* site's machines.  So the appliance list is discovered,
not predicted, and the operator renames the clusters afterwards if they care.

Why leader clustering rather than DBSCAN or k-means
---------------------------------------------------
Three reasons, all practical:

  * It is online.  Events arrive one at a time for weeks; we cannot re-fit over
    a growing history on a board that also has to serve a dashboard.
  * It needs no k.  We do not know how many machines the site has, and that is
    precisely the question being asked.
  * It is interpretable.  A cluster is a running mean and a count, so when the
    dashboard claims "this is the compressor" there is something a human can
    check.

A periodic batch consolidation pass merges clusters that have drifted together,
which recovers most of what a global method would have given us anyway.

Pairing rises with falls
------------------------
A real machine that switches on must eventually switch off with an
approximately equal and opposite signature.  Requiring that pairing is the
single most effective filter against phantom appliances: noise produces
unbalanced clusters, machines do not.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np

from ..config import CONFIG
from ..pipeline.events import Edge, EdgeType


# --------------------------------------------------------------------------- #
# Appliance type inference
# --------------------------------------------------------------------------- #
def infer_kind(delta_p: float, delta_q: float, inrush_ratio: float) -> str:
    """
    Guess what sort of load a signature represents.

    Not machine learning, just physics with thresholds - and honest about it.
    Displacement power factor and inrush behaviour separate the common
    industrial load classes well enough to be useful on a dashboard, and being
    a rule means a wrong answer is debuggable.
    """
    p, q = abs(delta_p), abs(delta_q)
    pf = p / math.hypot(p, q) if math.hypot(p, q) > 1e-6 else 1.0

    if inrush_ratio >= 2.0 and pf < 0.92:
        return "induction motor"
    if pf >= 0.97 and inrush_ratio < 1.4:
        return "resistive heating"
    if pf >= 0.95 and inrush_ratio >= 1.8:
        return "switched-mode supply"
    if pf < 0.75:
        return "reactive load"
    return "mixed load"


# --------------------------------------------------------------------------- #
@dataclass
class DiscoveredAppliance:
    """One machine the node believes exists, inferred from switching events."""

    id: int
    label: str

    # Cluster centroid, in the same units as the edges.
    mean_dp: float = 0.0
    mean_dq: float = 0.0
    m2_dp: float = 0.0              # Welford accumulator for variance
    observations: int = 0

    rise_count: int = 0
    fall_count: int = 0
    mean_inrush: float = 1.0

    # Live state.
    is_on: bool = False
    last_on_t: float = 0.0
    last_off_t: float = 0.0

    # Lifetime statistics.
    total_runtime_s: float = 0.0
    total_energy_wh: float = 0.0
    cycles: int = 0
    #: Duration of each completed run, used to learn what "normal" looks like.
    run_durations: list[float] = field(default_factory=list)

    first_seen_t: float = 0.0
    last_seen_t: float = 0.0
    user_named: bool = False

    # ------------------------------------------------------------------ #
    @property
    def std_dp(self) -> float:
        if self.observations < 2:
            return 0.0
        return math.sqrt(max(0.0, self.m2_dp / (self.observations - 1)))

    @property
    def kind(self) -> str:
        return infer_kind(self.mean_dp, self.mean_dq, self.mean_inrush)

    @property
    def power_factor(self) -> float:
        s = math.hypot(self.mean_dp, self.mean_dq)
        return abs(self.mean_dp) / s if s > 1e-6 else 1.0

    @property
    def balanced(self) -> bool:
        """
        True when rises and falls roughly match.

        An unbalanced cluster is almost always noise or a mis-split machine, so
        this gates whether the appliance is shown to the operator at all.
        """
        total = self.rise_count + self.fall_count
        if total < 4:
            return False
        minority = min(self.rise_count, self.fall_count)
        return minority / total >= 0.30

    @property
    def confirmed(self) -> bool:
        return (self.observations >= CONFIG.learning.min_observations
                and self.balanced)

    @property
    def mean_run_s(self) -> float:
        return float(np.mean(self.run_durations)) if self.run_durations else 0.0

    def matches(self, edge: Edge) -> float:
        """
        Distance from this cluster to an edge, or ``inf`` if out of tolerance.

        Tolerance is both absolute and proportional: a fixed band alone would
        be far too tight for a 2 kW machine and far too loose for a 100 W one.
        """
        cfg = CONFIG.learning
        dp, dq = abs(edge.delta_p), abs(edge.delta_q)
        cp, cq = abs(self.mean_dp), abs(self.mean_dq)

        tol_p = max(cfg.cluster_tolerance_w, cfg.cluster_tolerance_frac * cp)
        tol_q = max(cfg.cluster_tolerance_w, cfg.cluster_tolerance_frac * max(cq, cp * 0.3))

        if abs(dp - cp) > tol_p or abs(dq - cq) > tol_q:
            return math.inf
        # Normalised so P and Q contribute comparably despite different scales.
        return math.hypot((dp - cp) / tol_p, (dq - cq) / tol_q)

    #: Floor on the centroid update rate.  A pure running mean stops moving
    #: once a cluster has hundreds of observations, and that breaks machine
    #: identity: as a motor degrades it draws more power and worse power
    #: factor, drifts outside its own cluster's tolerance, and gets filed as a
    #: brand-new appliance - taking its health history with it. Keeping a floor
    #: under the learning rate lets a confirmed cluster follow its machine as
    #: it ages, which is exactly the behaviour we want, while the health model
    #: keeps comparing against its frozen original baseline.
    DRIFT_ALPHA: float = 0.03

    def absorb(self, edge: Edge) -> None:
        """Fold an edge into the cluster, letting the centroid track slow drift."""
        dp, dq = abs(edge.delta_p), abs(edge.delta_q)
        self.observations += 1
        n = self.observations
        alpha = max(1.0 / n, self.DRIFT_ALPHA)

        delta = dp - abs(self.mean_dp)
        self.mean_dp = abs(self.mean_dp) + alpha * delta
        # Welford's update is no longer exact once alpha is floored, so this
        # tracks a decaying spread instead. It is only used for display.
        self.m2_dp = (1 - alpha) * self.m2_dp + alpha * delta * delta * max(1, n - 1) / max(1, n)

        self.mean_dq += alpha * (dq - self.mean_dq)
        if edge.type is EdgeType.RISE:
            self.rise_count += 1
            r_alpha = max(1.0 / max(1, self.rise_count), self.DRIFT_ALPHA)
            self.mean_inrush += r_alpha * (edge.inrush_ratio - self.mean_inrush)
        else:
            self.fall_count += 1

        self.last_seen_t = edge.timestamp
        if not self.first_seen_t:
            self.first_seen_t = edge.timestamp

    def to_dict(self) -> dict[str, Any]:
        blob = asdict(self)
        blob.pop("m2_dp", None)
        blob.pop("run_durations", None)
        blob.update({
            "std_dp": self.std_dp,
            "kind": self.kind,
            "power_factor": self.power_factor,
            "confirmed": self.confirmed,
            "mean_run_s": self.mean_run_s,
        })
        return blob


# --------------------------------------------------------------------------- #
class NILMEngine:
    """Discovers and tracks appliances from a stream of edges."""

    #: Smallest step that may found a new appliance. Existing clusters still
    #: accept smaller edges; this only governs creation.
    min_cluster_w: float = 60.0

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or CONFIG.learning
        self.appliances: list[DiscoveredAppliance] = []
        self._next_id = 1
        self._label_seq = 0
        #: Edges we could not attribute, kept for the consolidation pass.
        self._unmatched: list[Edge] = []
        self._last_consolidate = 0.0
        self._last_update_t = 0.0

    # ------------------------------------------------------------------ #
    def _new_label(self) -> str:
        """Load A, Load B, ... AA, AB - operator renames them later."""
        n = self._label_seq
        self._label_seq += 1
        letters = ""
        while True:
            letters = chr(ord("A") + n % 26) + letters
            n = n // 26 - 1
            if n < 0:
                break
        return f"Load {letters}"

    def _create(self, edge: Edge) -> DiscoveredAppliance:
        app = DiscoveredAppliance(
            id=self._next_id,
            label=self._new_label(),
            mean_dp=abs(edge.delta_p),
            mean_dq=abs(edge.delta_q),
            first_seen_t=edge.timestamp,
        )
        self._next_id += 1
        self.appliances.append(app)
        return app

    # ------------------------------------------------------------------ #
    def push(self, edge: Edge) -> DiscoveredAppliance | None:
        """
        Attribute one edge to an appliance, creating one if nothing fits.

        Returns the appliance the edge was assigned to.
        """
        self._last_update_t = edge.timestamp

        best: DiscoveredAppliance | None = None
        best_d = math.inf
        for app in self.appliances:
            d = app.matches(edge)
            if d < best_d:
                best_d, best = d, app

        if best is None and abs(edge.delta_p) < self.min_cluster_w:
            # Too small to be worth a cluster of its own. Creating one for
            # every marginal step is how the appliance list fills with 45 W
            # phantoms that never confirm but do consume slots.
            return None

        if best is None:
            if len(self.appliances) >= self.cfg.max_appliances:
                # Out of slots: park it for consolidation rather than forcing a
                # bad match, which would corrupt an existing centroid.
                self._unmatched.append(edge)
                del self._unmatched[:-500]
                return None
            best = self._create(edge)

        best.absorb(edge)
        self._apply_state(best, edge)
        return best

    def expire_stale(self, now: float) -> int:
        """
        Mark machines off when their stop event was clearly missed.

        The detector loses an edge whenever a machine switches while a larger
        one is masking it, and a missed stop leaves an appliance believed to be
        running forever. That poisons the residual - it was measured at
        -3781 W after a seven day replay, meaning the model thought nearly four
        kilowatts more was running than the meter could see - and it inflates
        runtime and energy for every machine it happens to.

        A machine that has been on for far longer than it has ever run before
        is the signature of a lost edge, not of a long shift, so we close the
        cycle without crediting energy we cannot vouch for.
        """
        closed = 0
        for app in self.appliances:
            if not app.is_on or not app.last_on_t:
                continue
            typical = app.mean_run_s or 1800.0
            if (now - app.last_on_t) > max(4.0 * typical, 6 * 3600.0):
                app.is_on = False
                app.last_off_t = now
                closed += 1
        return closed

    def _apply_state(self, app: DiscoveredAppliance, edge: Edge) -> None:
        """Update on/off state and accumulate runtime and energy."""
        if edge.type is EdgeType.RISE:
            if not app.is_on:
                app.is_on = True
                app.last_on_t = edge.timestamp
        else:
            if app.is_on:
                app.is_on = False
                app.last_off_t = edge.timestamp
                run = max(0.0, edge.timestamp - app.last_on_t)
                # Guard against a missed rise producing an absurd run length.
                if 0.0 < run < 24 * 3600:
                    app.total_runtime_s += run
                    app.total_energy_wh += abs(app.mean_dp) * run / 3600.0
                    app.cycles += 1
                    app.run_durations.append(run)
                    del app.run_durations[:-200]

    # ------------------------------------------------------------------ #
    def consolidate(self, now: float | None = None, force: bool = False) -> int:
        """
        Merge clusters that have drifted into each other and drop noise.

        Online clustering can split one machine across two clusters when its
        first few observations happen to straddle the tolerance band. This pass
        repairs that. Returns the number of merges performed.
        """
        now = now if now is not None else time.time()
        if not force and now - self._last_consolidate < 900.0:
            return 0
        self._last_consolidate = now

        merges = self.expire_stale(now)
        changed = True
        while changed:
            changed = False
            for i, a in enumerate(self.appliances):
                for b in self.appliances[i + 1:]:
                    if self._should_merge(a, b):
                        self._merge(a, b)
                        self.appliances.remove(b)
                        merges += 1
                        changed = True
                        break
                if changed:
                    break

        # Drop clusters that never became credible and have gone quiet. An
        # appliance that was seen twice a week ago was noise.
        stale_cutoff = now - 6 * 3600
        before = len(self.appliances)
        self.appliances = [
            a for a in self.appliances
            if a.confirmed or a.last_seen_t > stale_cutoff or a.user_named
        ]
        merges += before - len(self.appliances)
        return merges

    def _should_merge(self, a: DiscoveredAppliance, b: DiscoveredAppliance) -> bool:
        cfg = self.cfg
        scale = max(abs(a.mean_dp), abs(b.mean_dp), 1.0)
        tol_p = max(cfg.cluster_tolerance_w, cfg.cluster_tolerance_frac * scale)
        tol_q = max(cfg.cluster_tolerance_w, cfg.cluster_tolerance_frac * scale)
        if abs(abs(a.mean_dp) - abs(b.mean_dp)) > tol_p:
            return False
        if abs(a.mean_dq - b.mean_dq) > tol_q:
            return False
        # Never merge two clusters that are both already well established and
        # disagree on inrush - that is two genuinely different machines of
        # similar size, which is exactly the case we must not collapse.
        if a.observations > 10 and b.observations > 10:
            if abs(a.mean_inrush - b.mean_inrush) > 0.8:
                return False
        return True

    def _merge(self, keep: DiscoveredAppliance, drop: DiscoveredAppliance) -> None:
        total = keep.observations + drop.observations
        if total:
            w_k = keep.observations / total
            w_d = drop.observations / total
            keep.mean_dp = keep.mean_dp * w_k + drop.mean_dp * w_d
            keep.mean_dq = keep.mean_dq * w_k + drop.mean_dq * w_d
            keep.mean_inrush = keep.mean_inrush * w_k + drop.mean_inrush * w_d
        keep.m2_dp += drop.m2_dp
        keep.observations = total
        keep.rise_count += drop.rise_count
        keep.fall_count += drop.fall_count
        keep.total_runtime_s += drop.total_runtime_s
        keep.total_energy_wh += drop.total_energy_wh
        keep.cycles += drop.cycles
        keep.run_durations.extend(drop.run_durations)
        del keep.run_durations[:-200]
        keep.first_seen_t = min(keep.first_seen_t or drop.first_seen_t,
                                drop.first_seen_t or keep.first_seen_t)
        keep.last_seen_t = max(keep.last_seen_t, drop.last_seen_t)
        if drop.user_named and not keep.user_named:
            keep.label, keep.user_named = drop.label, True

    # ------------------------------------------------------------------ #
    def rename(self, appliance_id: int, label: str) -> bool:
        for a in self.appliances:
            if a.id == appliance_id:
                a.label, a.user_named = label, True
                return True
        return False

    def confirmed_appliances(self) -> list[DiscoveredAppliance]:
        return sorted((a for a in self.appliances if a.confirmed),
                      key=lambda a: -abs(a.mean_dp))

    def explained_power(self) -> float:
        """Total power attributable to appliances believed to be running."""
        return sum(abs(a.mean_dp) for a in self.appliances if a.is_on)

    def residual(self, measured_p: float) -> float:
        """
        Power we cannot account for.

        A persistently large residual is the honest signal that the appliance
        model is incomplete - loads too small to clear the detection threshold,
        or machines that ramp instead of switching. Surfaced on the dashboard
        rather than hidden, because a NILM system that silently under-reports
        is worse than one that admits what it missed.
        """
        return measured_p - self.explained_power()

    def snapshot(self) -> dict[str, Any]:
        confirmed = self.confirmed_appliances()
        return {
            "appliances": [a.to_dict() for a in confirmed],
            "candidate_count": len(self.appliances) - len(confirmed),
            "explained_w": self.explained_power(),
            "updated_t": self._last_update_t,
        }
