"""Message queue: RabbitMQ when RABBITMQ_URL is set, otherwise an in-process queue.

Only event ids travel through the queue; the event itself lives in the outbox table.
Consumers are idempotent (they skip events already marked processed), so RabbitMQ's
at-least-once delivery can't cause double work.
"""
from __future__ import annotations

import collections
import json
import logging
import threading

log = logging.getLogger("carequeue.broker")
QUEUE = "carequeue.events"


class InMemoryBroker:
    kind = "in-memory"

    def __init__(self) -> None:
        self._q: collections.deque[int] = collections.deque()
        self._lock = threading.Lock()

    def publish(self, event_id: int) -> None:
        with self._lock:
            self._q.append(event_id)

    def drain(self, handler, limit: int = 1000) -> int:
        done = 0
        while done < limit:
            with self._lock:
                if not self._q:
                    break
                event_id = self._q.popleft()
            handler(event_id)
            done += 1
        return done

    def pending(self) -> int:
        return len(self._q)


class RabbitMQBroker:
    kind = "rabbitmq"

    def __init__(self, url: str):
        self.url = url
        self._local = threading.local()  # pika connections aren't thread-safe: one per thread

    def _channel(self):
        import pika

        ch = getattr(self._local, "channel", None)
        if ch is None or ch.is_closed:
            conn = pika.BlockingConnection(pika.URLParameters(self.url))
            ch = conn.channel()
            ch.queue_declare(queue=QUEUE, durable=True)
            self._local.conn, self._local.channel = conn, ch
        return ch

    def publish(self, event_id: int) -> None:
        import pika

        self._channel().basic_publish(
            exchange="", routing_key=QUEUE, body=json.dumps({"event_id": event_id}),
            properties=pika.BasicProperties(delivery_mode=2),  # persistent
        )

    def drain(self, handler, limit: int = 1000) -> int:
        """Process whatever is queued right now, acking each message after it's handled."""
        ch = self._channel()
        done = 0
        while done < limit:
            method, _props, body = ch.basic_get(queue=QUEUE, auto_ack=False)
            if method is None:
                break
            try:
                handler(json.loads(body)["event_id"])
                ch.basic_ack(method.delivery_tag)
            except Exception:
                log.exception("event handling failed; message requeued")
                ch.basic_nack(method.delivery_tag, requeue=True)
                raise
            done += 1
        return done

    def pending(self) -> int:
        return self._channel().queue_declare(queue=QUEUE, durable=True, passive=True).method.message_count


def make_broker(rabbitmq_url: str | None):
    return RabbitMQBroker(rabbitmq_url) if rabbitmq_url else InMemoryBroker()
