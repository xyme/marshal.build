"""Cross-task event relay (platform-scale-policies spec R3.2).

The in-memory buses (deployment, codegen, specgen) fan events to SSE
subscribers ON THIS TASK only. With desiredCount > 1 the subscriber and the
producer may live on different tasks — this relay mirrors every local publish
to Postgres NOTIFY and re-publishes inbound notifications locally, keeping the
bus API unchanged for every producer and consumer.

Design points:
  - One channel (`marshal_events`), JSON payload {src, bus, key, event}.
  - Self-skip via a per-process id — a task never re-publishes its own events.
  - SQLite / relay-down → local-only mode (Alpha behavior), never an error on
    the publish path (fail-open family).
  - NOTIFY payloads are capped (~8KB); oversized events stay local-only and
    are logged. Persisted timelines cover replay for late subscribers.
"""

import asyncio
import contextlib
import json
import logging
import uuid

logger = logging.getLogger("marshal.event_relay")

CHANNEL = "marshal_events"
MAX_PAYLOAD_BYTES = 7500  # postgres NOTIFY hard limit is 8000
TASK_ID = uuid.uuid4().hex  # unique per process


def _pg_dsn() -> str | None:
    """asyncpg DSN from settings, or None when not Postgres (local-only mode)."""
    from app.core.config import get_settings

    url = get_settings().database_url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        return url
    return None


class EventRelay:
    def __init__(self) -> None:
        self._buses: dict[str, object] = {}
        self._local_publish: dict[str, object] = {}  # name -> original bound publish
        self._conn = None
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._stopping = False

    # ------------------------------------------------------------ wiring

    def wire(self, name: str, bus) -> None:
        """Wrap `bus.publish` so local publishes also NOTIFY other tasks."""
        if name in self._buses:
            return
        self._buses[name] = bus
        original = bus.publish
        self._local_publish[name] = original

        async def relayed_publish(key: str, event: dict, _orig=original, _name=name):
            await _orig(key, event)
            await self._notify(_name, key, event)

        bus.publish = relayed_publish  # instance attribute shadows the method

    async def start(self) -> None:
        dsn = _pg_dsn()
        if dsn is None:
            logger.info("event relay: non-postgres database — local-only mode")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(dsn))

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------ outbound

    async def _notify(self, bus_name: str, key: str, event: dict) -> None:
        conn = self._conn
        if conn is None:
            return  # relay down / local-only — subscribers on this task still got it
        try:
            payload = json.dumps(
                {"src": TASK_ID, "bus": bus_name, "key": key, "event": event},
                default=str,
            )
        except (TypeError, ValueError):
            logger.warning("event relay: unserializable event on %s/%s", bus_name, key)
            return
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            logger.warning(
                "event relay: oversized event (%s/%s) stays local-only", bus_name, key
            )
            return
        try:
            async with self._send_lock:
                await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, payload)
        except Exception as exc:  # noqa: BLE001 — never break the publish path
            logger.warning("event relay notify failed (local-only until reconnect): %s", exc)

    # ------------------------------------------------------------ inbound

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if data.get("src") == TASK_ID:
            return  # our own publish — local subscribers already have it
        publish = self._local_publish.get(data.get("bus", ""))
        if publish is None or "key" not in data:
            return
        asyncio.get_running_loop().create_task(publish(str(data["key"]), data.get("event") or {}))

    # ------------------------------------------------------------ lifecycle

    async def _run(self, dsn: str) -> None:
        import asyncpg

        backoff = 1.0
        while not self._stopping:
            try:
                conn = await asyncpg.connect(dsn, timeout=10)
                await conn.add_listener(CHANNEL, self._on_notify)
                self._conn = conn
                backoff = 1.0
                logger.info("event relay: listening on %s (task %s)", CHANNEL, TASK_ID[:8])
                while not self._stopping:
                    await asyncio.sleep(15)
                    async with self._send_lock:
                        await conn.execute("SELECT 1")  # liveness; raises on dead conn
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._conn = None
                logger.warning("event relay connection lost (%s); retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        self._conn = None


RELAY = EventRelay()
