"""Redis and RabbitMQ adapters. These run when the services are available (CI starts both)."""
import os
from datetime import date, datetime

import pytest

from carequeue.broker import InMemoryBroker, RabbitMQBroker
from carequeue.cache import MemoryScheduleCache, RedisScheduleCache

from .conftest import PATIENTS
from .helpers import held_for, slot_at

REDIS = os.getenv("CQ_TEST_REDIS_URL")
RABBIT = os.getenv("CQ_TEST_RABBITMQ_URL")
DAY = date(2026, 10, 13)


def test_memory_cache_expires(monkeypatch):
    cache = MemoryScheduleCache(ttl_s=10)
    cache.set(1, DAY, [{"id": 1}])
    assert cache.get(1, DAY) == [{"id": 1}]
    import carequeue.cache as mod

    real = mod.time.monotonic
    monkeypatch.setattr(mod.time, "monotonic", lambda: real() + 11)
    assert cache.get(1, DAY) is None


@pytest.mark.skipif(not REDIS, reason="set CQ_TEST_REDIS_URL")
def test_redis_cache_roundtrip_and_invalidation():
    cache = RedisScheduleCache(REDIS, ttl_s=30)
    cache.clear()
    assert cache.get(1, DAY) is None
    cache.set(1, DAY, [{"id": 7, "state": "open"}])
    assert cache.get(1, DAY) == [{"id": 7, "state": "open"}]
    cache.invalidate(1, DAY)
    assert cache.get(1, DAY) is None


@pytest.mark.skipif(not REDIS, reason="set CQ_TEST_REDIS_URL")
def test_clinic_with_redis_cache(clinic):
    clinic.cache = RedisScheduleCache(REDIS, ttl_s=30)
    clinic.cache.clear()
    assert clinic.schedule(1, DAY)["source"] == "database"
    assert clinic.schedule(1, DAY)["source"] == "cache"
    clinic.book(PATIENTS["Ava"], slot_at(clinic, 1, datetime(2026, 10, 13, 10, 0)))
    assert clinic.schedule(1, DAY)["source"] == "database"


def test_redis_outage_is_a_cache_miss():
    cache = RedisScheduleCache("redis://127.0.0.1:1/0", ttl_s=30)  # nothing listens on port 1
    assert cache.get(1, DAY) is None
    cache.set(1, DAY, [])  # silently ignored
    cache.invalidate(1, DAY)


def test_in_memory_broker_is_fifo():
    broker, seen = InMemoryBroker(), []
    for i in range(3):
        broker.publish(i)
    assert broker.drain(seen.append) == 3 and seen == [0, 1, 2] and broker.pending() == 0


@pytest.mark.skipif(not RABBIT, reason="set CQ_TEST_RABBITMQ_URL")
def test_waitlist_refill_through_rabbitmq(clinic):
    clinic.broker = RabbitMQBroker(RABBIT)
    clinic.broker.drain(lambda _id: None)  # empty the queue left by earlier runs
    for s in clinic.schedule(1, DAY)["slots"]:
        if s["state"] == "open":
            clinic.book(PATIENTS["Liam"], s["id"])
    clinic.join_waitlist(PATIENTS["Ava"], DAY, DAY, provider_id=1)
    slot = slot_at(clinic, 1, datetime(2026, 10, 13, 10, 0))
    clinic.cancel(next(a for a in clinic.appointments() if a["slot_id"] == slot)["id"])
    clinic.run_pending()
    assert held_for(clinic, PATIENTS["Ava"])[0]["slot_id"] == slot
    assert clinic.broker.pending() == 0
