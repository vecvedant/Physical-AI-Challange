"""
App Lab entry point — what runs on the UNO Q's Linux side.

App Lab executes this file on the MPU after uploading the sketch to the STM32.
Everything it does lives in the ``udyogiq`` package one directory up, which is
deliberate: the package stays importable and testable on a laptop with no
Arduino tooling installed, and this file is only the small amount of glue that
knows it is running on a board.

Transport
---------
Defaults to the Bridge, because on the board the STM32 owns the RS485 bus. Set
UDYOGIQ_METER_TRANSPORT=serial to have Linux master Modbus directly through a
USB-RS485 adapter — useful during bring-up, since it takes the sketch out of the
loop entirely and tells you whether a problem is wiring or firmware. Set it to
``sim`` to run the whole thing with no meter attached at all.

Why this does not use App.run()
-------------------------------
The App Lab idiom is ``App.run(user_loop=...)``, which drives a callback in a
loop. This project already owns its own scheduling: one acquisition thread at a
fixed cadence, with retraining, dispatch and housekeeping on cooperative timers
inside it. Handing that to an external loop driver would mean two schedulers
disagreeing about who owns the clock. So the node is started directly and
uvicorn is left to block in the foreground, which is the same shape the process
has when run from a terminal.
"""

import logging
import os
import signal
import sys
from pathlib import Path

# The package sits alongside python/, not inside it.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from udyogiq.api.server import serve          # noqa: E402
from udyogiq.config import CONFIG             # noqa: E402
from udyogiq.runtime import UdyogIQ           # noqa: E402

log = logging.getLogger("udyogiq.main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # On the board the MCU masters Modbus, so the Bridge is the right default.
    # An explicit environment setting always wins.
    transport = os.environ.get("UDYOGIQ_METER_TRANSPORT", "bridge")

    node = UdyogIQ(transport)

    # Optional: replay simulated history so a fresh board comes up already
    # knowing a plant. Only meaningful on the simulated transport, and off by
    # default - on real hardware the node must learn the actual site.
    warmup_days = float(os.environ.get("UDYOGIQ_WARMUP_DAYS", "0") or 0)
    if warmup_days > 0 and transport == "sim":
        node.warmup(days=warmup_days)

    node.start()

    # App Lab stops an app by signalling the process. Without a handler the
    # historian's pending writes are lost and SQLite is left with a hot
    # journal, so shutdown is explicit.
    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        node.stop()
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):        # not the main thread, or unsupported
            pass

    log.info("Udyog IQ on %s — dashboard at http://<board-ip>:%d",
             transport, CONFIG.server.port)
    try:
        serve(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
