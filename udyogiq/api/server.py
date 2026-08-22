"""
Dashboard server: FastAPI, a WebSocket, and the PWA served straight off the MPU.

Everything here is read-mostly.  The two endpoints that do mutate state -
renaming a discovered appliance and engaging manual override - are the only
ways a user can affect the plant through this interface, and neither can switch
anything on.  Turning actuation *on* is a config change requiring a restart,
deliberately: an HTTP endpoint that can energise a contactor is a bad idea on a
workshop LAN.

There is no authentication, and that is a considered decision rather than an
oversight.  The node serves on the local network only, has no route to the
internet, and adding a login to a device an operator needs to glance at with
oily hands is how it ends up with the password written on the enclosure. The
deployment guidance is to put it on the plant's own network segment. If this
ever gains a route from outside, it needs a reverse proxy with auth in front,
and that is documented rather than half-implemented here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import CONFIG, REPO_ROOT

log = logging.getLogger(__name__)

WEB_DIR = REPO_ROOT / "web"


def build_app(node) -> Any:
    """Construct the FastAPI application bound to a running node."""
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Udyog IQ", version="2.0.0", docs_url="/api/docs")

    # ------------------------------------------------------------------ #
    @app.get("/api/snapshot")
    def snapshot() -> JSONResponse:
        return JSONResponse(node.snapshot())

    @app.get("/api/history")
    def history(hours: float = 6.0, resolution: str = "minute") -> JSONResponse:
        """
        Historical series for the charts.

        Minute resolution by default because a phone plotting six hours of
        1 Hz samples is 21,600 points it will render badly and download slowly.
        Raw is available for a short window when someone is actually debugging.
        """
        hours = max(0.05, min(hours, 24 * 30))
        if resolution == "raw":
            return JSONResponse({
                "resolution": "raw",
                "samples": node.historian.recent_samples(
                    seconds=min(hours * 3600, 3600)),
            })
        return JSONResponse({
            "resolution": "minute",
            "samples": node.historian.minute_series(hours=hours),
        })

    @app.get("/api/appliances")
    def appliances() -> JSONResponse:
        health_by_id = {s.get("appliance_id"): s for s in node.health.states()}
        out = []
        for app_ in node.nilm.confirmed_appliances():
            blob = app_.to_dict()
            state = health_by_id.get(app_.id, {})
            blob["health"] = state
            monitor = node.health._monitors.get(app_.id)
            blob["health_reasons"] = monitor.explain() if monitor else []
            out.append(blob)
        return JSONResponse({"appliances": out,
                             "candidates": node.nilm.snapshot()["candidate_count"]})

    @app.post("/api/appliances/{appliance_id}/label")
    async def rename(appliance_id: int, payload: dict) -> JSONResponse:
        """
        Give a discovered cluster a human name.

        NILM can tell that a 1.5 kW motor with a 3x inrush exists; only the
        person who works there knows it is the compressor. Names survive
        cluster merges.
        """
        label = str(payload.get("label", "")).strip()
        if not label:
            raise HTTPException(status_code=400, detail="label is required")
        if len(label) > 60:
            raise HTTPException(status_code=400, detail="label is too long")
        if not node.nilm.rename(appliance_id, label):
            raise HTTPException(status_code=404, detail="no such appliance")
        return JSONResponse({"ok": True, "id": appliance_id, "label": label})

    @app.get("/api/decisions")
    def decisions(limit: int = 50) -> JSONResponse:
        return JSONResponse({
            "decisions": node.historian.recent_decisions(min(limit, 500))})

    @app.get("/api/dispatch")
    def dispatch() -> JSONResponse:
        plan = node.mpc.plan
        return JSONResponse({
            "plan": plan.to_dict(limit=96) if plan else None,
            "battery": node.battery.status(),
            "tariff": node.tariff.describe(),
            "mpc": {"solve_count": node.mpc.solve_count,
                    "last_solved_t": node.mpc.last_solved_t},
        })

    @app.get("/api/savings")
    def savings() -> JSONResponse:
        return JSONResponse({
            "counterfactual": node.counterfactual.snapshot(),
            "accounting": node.accountant.snapshot(),
            "demand": node.demand.status(),
        })

    @app.post("/api/policy/override")
    async def override(payload: dict) -> JSONResponse:
        """
        Hand control back to a human.

        Nothing automatic ever clears this - a person engaged it and a person
        clears it. That is the point of an override.
        """
        engaged = bool(payload.get("engaged", True))
        node.policy.set_manual_override(engaged)
        return JSONResponse({"ok": True, "manual_override": engaged})

    @app.get("/api/health")
    def healthcheck() -> JSONResponse:
        """Liveness probe: is the node still actually acquiring?"""
        snap = node.snapshot()
        acq = snap["acquisition"]
        live = snap["live"]
        stale_s = time.time() - (live.get("timestamp") or 0)
        ok = acq["online"] and stale_s < 30.0
        return JSONResponse(
            {"ok": ok, "stale_s": round(stale_s, 1), **acq},
            status_code=200 if ok else 503)

    # ------------------------------------------------------------------ #
    @app.websocket("/ws")
    async def stream(ws: WebSocket) -> None:
        """
        Push a snapshot at the configured rate.

        One serialisation is shared by every connected client, so a second
        phone costs a send rather than another pass over the whole state.
        """
        await ws.accept()
        period = 1.0 / max(CONFIG.server.broadcast_hz, 0.1)
        try:
            while True:
                await ws.send_text(json.dumps(node.snapshot(), default=float))
                await asyncio.sleep(period)
        except WebSocketDisconnect:
            pass
        except Exception as exc:                            # noqa: BLE001
            log.debug("websocket closed: %s", exc)

    # ------------------------------------------------------------------ #
    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        page = WEB_DIR / "index.html"
        if not page.exists():
            return HTMLResponse(
                "<h1>Udyog IQ</h1><p>Dashboard assets are missing. "
                "The API is still available at <a href='/api/docs'>/api/docs</a>.</p>",
                status_code=200)
        return FileResponse(page)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app


def serve(node, host: str | None = None, port: int | None = None) -> None:
    """Run the dashboard server in the foreground."""
    import uvicorn

    host = host or CONFIG.server.host
    port = port or CONFIG.server.port
    app = build_app(node)
    log.info("dashboard on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
