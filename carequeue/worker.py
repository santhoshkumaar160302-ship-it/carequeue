"""Standalone worker: publishes outbox events, consumes the queue and expires holds.

With RabbitMQ (docker compose), the API runs with CQ_BACKGROUND_WORKER=0 and this
process does the background work, so it can be scaled or restarted on its own:

    python -m carequeue.worker
"""
from __future__ import annotations

import logging
import signal
import threading

from .app import build_services

log = logging.getLogger("carequeue.worker")


def main(interval_s: float = 0.5) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    services = build_services()
    clinic = services.clinic
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("worker started (queue: %s, cache: %s)", clinic.broker.kind, clinic.cache.backend)
    while not stop.is_set():
        try:
            done = clinic.run_pending(max_rounds=10)
            if done:
                log.info("processed %d items", done)
        except Exception:  # noqa: BLE001 - log and retry on the next pass
            log.exception("worker pass failed")
        stop.wait(interval_s)
    log.info("worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
