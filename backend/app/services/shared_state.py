"""Shared runtime state (platform-scale-policies spec R3.1).

Atomic counters on DynamoDB (`marshal-runtime-state`, PAY_PER_REQUEST, TTL on
`expires`) so rate buckets and spend accounting are correct across N backend
tasks. RUNTIME_STATE_TABLE unset → an in-process backend with identical
semantics (tests, local dev, single-task fallback).

Fail-open discipline (S5): DynamoDB trouble logs, alerts once, and returns
values that DISABLE enforcement rather than break the request path.
"""

import asyncio
import logging
import os
import threading
import time

import boto3

logger = logging.getLogger("marshal.shared_state")

_TABLE_ENV = "RUNTIME_STATE_TABLE"


class _InProcessBackend:
    """Dict-backed twin of the DDB semantics (single task / tests)."""

    def __init__(self) -> None:
        self._data: dict[str, int] = {}
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def _sweep(self) -> None:
        now = time.time()
        for key in [k for k, exp in self._expiry.items() if exp <= now]:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    def add_and_get(self, key: str, amount: int, ttl_s: int | None = None) -> int:
        with self._lock:
            self._sweep()
            value = self._data.get(key, 0) + amount
            self._data[key] = value
            if ttl_s is not None:
                self._expiry[key] = time.time() + ttl_s
            return value

    def get(self, key: str) -> int:
        with self._lock:
            self._sweep()
            return self._data.get(key, 0)

    def seed_if_absent(self, key: str, value: int) -> None:
        with self._lock:
            self._sweep()
            self._data.setdefault(key, value)

    def put(self, key: str, value: int) -> None:
        """Overwrite a counter (test seam / admin backfill)."""
        with self._lock:
            self._sweep()
            self._data[key] = value

    def reset(self) -> None:  # tests
        with self._lock:
            self._data.clear()
            self._expiry.clear()


class _DynamoBackend:
    def __init__(self, table: str) -> None:
        from app.core.config import get_settings

        self.table = table
        self.client = boto3.client("dynamodb", region_name=get_settings().aws_region)

    def add_and_get(self, key: str, amount: int, ttl_s: int | None = None) -> int:
        update = "ADD val :n"
        values = {":n": {"N": str(amount)}}
        names = None
        if ttl_s is not None:
            update += " SET #exp = if_not_exists(#exp, :exp)"
            values[":exp"] = {"N": str(int(time.time()) + ttl_s)}
            names = {"#exp": "expires"}
        kwargs = {
            "TableName": self.table,
            "Key": {"pk": {"S": key}},
            "UpdateExpression": update,
            "ExpressionAttributeValues": values,
            "ReturnValues": "UPDATED_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        out = self.client.update_item(**kwargs)
        return int(out["Attributes"]["val"]["N"])

    def get(self, key: str) -> int:
        out = self.client.get_item(
            TableName=self.table, Key={"pk": {"S": key}}, ConsistentRead=True
        )
        item = out.get("Item")
        if not item or "val" not in item:
            return 0
        expires = item.get("expires", {}).get("N")
        if expires and int(expires) <= int(time.time()):
            return 0  # logically expired; DDB TTL reaps lazily
        return int(item["val"]["N"])

    def seed_if_absent(self, key: str, value: int) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.put_item(
                TableName=self.table,
                Item={"pk": {"S": key}, "val": {"N": str(value)}},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


class SharedState:
    """Facade with fail-open error handling. All methods are async (DDB calls
    run in threads); the in-process backend answers inline."""

    def __init__(self) -> None:
        self._backend = None
        self._failed_once = False

    @property
    def backend(self):
        if self._backend is None:
            table = os.environ.get(_TABLE_ENV, "").strip()
            if table:
                self._backend = _DynamoBackend(table)
                logger.info("shared state: DynamoDB backend (%s)", table)
            else:
                self._backend = _InProcessBackend()
                logger.info("shared state: in-process backend")
        return self._backend

    def _fail_open(self, exc: Exception) -> None:
        if not self._failed_once:
            self._failed_once = True
            logger.exception("shared state unavailable — enforcement fails open")
        else:
            logger.warning("shared state error (fail-open): %s", exc)

    async def add_and_get(self, key: str, amount: int, ttl_s: int | None = None) -> int | None:
        try:
            if isinstance(self.backend, _InProcessBackend):
                return self.backend.add_and_get(key, amount, ttl_s)
            return await asyncio.to_thread(self.backend.add_and_get, key, amount, ttl_s)
        except Exception as exc:  # noqa: BLE001
            self._fail_open(exc)
            return None

    async def get(self, key: str) -> int | None:
        try:
            if isinstance(self.backend, _InProcessBackend):
                return self.backend.get(key)
            return await asyncio.to_thread(self.backend.get, key)
        except Exception as exc:  # noqa: BLE001
            self._fail_open(exc)
            return None

    async def seed_if_absent(self, key: str, value: int) -> None:
        try:
            if isinstance(self.backend, _InProcessBackend):
                self.backend.seed_if_absent(key, value)
                return
            await asyncio.to_thread(self.backend.seed_if_absent, key, value)
        except Exception as exc:  # noqa: BLE001
            self._fail_open(exc)

    def reset_for_tests(self) -> None:
        self._backend = None
        self._failed_once = False


STATE = SharedState()
