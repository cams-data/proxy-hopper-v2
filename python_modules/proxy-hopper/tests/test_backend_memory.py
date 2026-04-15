"""MemoryBackend tests — covers all Backend primitive groups.

Tests the generic Backend contract via the MemoryBackend implementation.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from proxy_hopper.backend.memory import MemoryBackend


@pytest.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


class TestLifecycle:
    async def test_start_stop(self):
        b = MemoryBackend()
        await b.start()
        await b.stop()


class TestClaimInit:
    async def test_first_caller_wins(self, backend):
        assert await backend.claim_init("k") is True

    async def test_second_caller_loses(self, backend):
        await backend.claim_init("k")
        assert await backend.claim_init("k") is False

    async def test_different_keys_independent(self, backend):
        assert await backend.claim_init("a") is True
        assert await backend.claim_init("b") is True


class TestQueue:
    async def test_push_pop(self, backend):
        await backend.queue_push("q", "val")
        result = await backend.queue_pop_blocking("q", timeout=1.0)
        assert result == "val"

    async def test_pop_timeout(self, backend):
        result = await backend.queue_pop_blocking("empty", timeout=0.05)
        assert result is None

    async def test_queue_size(self, backend):
        assert await backend.queue_size("q") == 0
        await backend.queue_push("q", "a")
        await backend.queue_push("q", "b")
        assert await backend.queue_size("q") == 2

    async def test_queue_push_many(self, backend):
        await backend.queue_push_many("q", ["a", "b", "c"])
        assert await backend.queue_size("q") == 3

    async def test_fifo_order(self, backend):
        await backend.queue_push_many("q", ["first", "second"])
        assert await backend.queue_pop_blocking("q", 0.1) == "first"
        assert await backend.queue_pop_blocking("q", 0.1) == "second"


class TestCounter:
    async def test_increment_starts_at_one(self, backend):
        assert await backend.counter_increment("c") == 1

    async def test_increment_accumulates(self, backend):
        await backend.counter_increment("c")
        await backend.counter_increment("c")
        assert await backend.counter_increment("c") == 3

    async def test_counter_set_and_get(self, backend):
        await backend.counter_set("c", 42)
        assert await backend.counter_get("c") == 42

    async def test_counter_get_missing_returns_zero(self, backend):
        assert await backend.counter_get("missing") == 0

    async def test_counter_set_zero_resets(self, backend):
        await backend.counter_increment("c")
        await backend.counter_set("c", 0)
        assert await backend.counter_get("c") == 0


class TestSortedSet:
    async def test_add_and_members(self, backend):
        await backend.sorted_set_add("z", "a", 1.0)
        await backend.sorted_set_add("z", "b", 2.0)
        members = await backend.sorted_set_members("z")
        assert set(members) == {"a", "b"}

    async def test_add_updates_score(self, backend):
        await backend.sorted_set_add("z", "a", 1.0)
        await backend.sorted_set_add("z", "a", 5.0)
        # score updated — still one member
        assert await backend.sorted_set_members("z") == ["a"]

    async def test_pop_by_max_score(self, backend):
        now = time.time()
        await backend.sorted_set_add("z", "expired", now - 1)
        await backend.sorted_set_add("z", "future", now + 100)
        popped = await backend.sorted_set_pop_by_max_score("z", now)
        assert popped == ["expired"]
        assert await backend.sorted_set_members("z") == ["future"]

    async def test_pop_by_max_score_empty_key(self, backend):
        assert await backend.sorted_set_pop_by_max_score("nope", 9999.0) == []

    async def test_members_missing_key(self, backend):
        assert await backend.sorted_set_members("nope") == []


class TestCompoundRead:
    async def test_queue_size_and_sorted_set_members(self, backend):
        await backend.queue_push("q", "v")
        await backend.sorted_set_add("z", "m", 1.0)
        size, members = await backend.queue_size_and_sorted_set_members("q", "z")
        assert size == 1
        assert members == ["m"]


class TestKV:
    async def test_set_and_get(self, backend):
        await backend.kv_set("key", "value")
        assert await backend.kv_get("key") == "value"

    async def test_get_missing_returns_none(self, backend):
        assert await backend.kv_get("nope") is None

    async def test_delete(self, backend):
        await backend.kv_set("key", "value")
        await backend.kv_delete("key")
        assert await backend.kv_get("key") is None

    async def test_delete_missing_is_noop(self, backend):
        await backend.kv_delete("nope")  # should not raise

    async def test_list_by_prefix(self, backend):
        await backend.kv_set("ph:a:x", "1")
        await backend.kv_set("ph:a:y", "2")
        await backend.kv_set("ph:b:z", "3")
        pairs = await backend.kv_list("ph:a:")
        assert dict(pairs) == {"ph:a:x": "1", "ph:a:y": "2"}

    async def test_list_empty_prefix(self, backend):
        assert await backend.kv_list("nothing") == []


class TestPubSub:
    async def test_publish_delivers_to_subscriber(self, backend):
        received = []

        async def listen():
            async with backend.subscribe("ch") as messages:
                async for msg in messages:
                    received.append(msg)
                    return  # exit after first message

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)  # let subscriber register
        await backend.publish("ch", "hello")
        await asyncio.wait_for(task, timeout=1.0)
        assert received == ["hello"]

    async def test_publish_to_no_subscribers_is_noop(self, backend):
        await backend.publish("empty-channel", "msg")  # should not raise

    async def test_multiple_subscribers_all_receive(self, backend):
        received_a: list[str] = []
        received_b: list[str] = []

        async def listen(store, out):
            async with backend.subscribe("ch") as messages:
                async for msg in messages:
                    out.append(msg)
                    return

        t1 = asyncio.create_task(listen(backend, received_a))
        t2 = asyncio.create_task(listen(backend, received_b))
        await asyncio.sleep(0)
        await backend.publish("ch", "broadcast")
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
        assert received_a == ["broadcast"]
        assert received_b == ["broadcast"]
