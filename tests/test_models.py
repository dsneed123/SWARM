from __future__ import annotations

import asyncio
import os
import time

import pytest

from swarm.config import Settings
from swarm.core.db import Database
from swarm.core.events import EventBus
from swarm.core.types import Tier
from swarm.hardware.budget import GB, ResourceBudget
from swarm.hardware.telemetry import HardwareMonitor, HardwareSample
from swarm.models.backend import ChatMessage
from swarm.models.fake import FakeBackend
from swarm.models.profiles import ProfileStore, assign_tiers
from swarm.models.router import ModelRequest, ModelRouter
from swarm.models.scheduler import ModelScheduler
from tests.conftest import requires_ollama


class FixedMonitor(HardwareMonitor):
    """Monitor that reports a fixed amount of free memory plus what we've allocated."""

    def __init__(self, total_gb: float, available_gb: float, budget: ResourceBudget | None = None):
        self.total = int(total_gb * GB)
        self.available = int(available_gb * GB)
        self.budget = budget
        super().__init__()

    def sample(self) -> HardwareSample:
        allocated = self.budget.allocated if self.budget else 0
        s = HardwareSample(
            ts=time.time(), mem_total=self.total, mem_available=self.available - allocated,
            mem_used=self.total - self.available + allocated, swap_used=0, cpu_percent=5.0, load1=0.5,
        )
        self._latest = s
        return s


def make_scheduler(tmp_path, models=None, total_gb=128, available_gb=110, ceiling=70, parallel=2, responder=None):
    backend = FakeBackend(models, responder=responder)
    db = Database(tmp_path / "state.db")
    profiles = ProfileStore(db)
    budget = ResourceBudget(ceiling_percent=ceiling, safety_reserve_gb=2)
    monitor = FixedMonitor(total_gb, available_gb, budget)
    settings = Settings(workspace=tmp_path)
    settings.ollama.num_parallel = parallel
    settings.hardware.memory_ceiling_percent = ceiling
    sched = ModelScheduler(backend, profiles, budget, monitor, EventBus(), settings)
    sched.reconcile_interval_s = 0.05
    return sched, backend, profiles


async def test_profiles_sync_assigns_tiers(tmp_path):
    sched, backend, profiles = make_scheduler(tmp_path)
    await sched.refresh_models()
    tiers = {p.name: p.tier for p in profiles.all()}
    assert tiers["small:7b"] == Tier.FAST
    assert tiers["medium:14b"] == Tier.STANDARD
    assert tiers["large:70b"] == Tier.DEEP


def test_assign_tiers_fills_gaps():
    from swarm.models.backend import ModelDescriptor
    from swarm.models.profiles import ModelProfile

    profs = [ModelProfile.from_descriptor(ModelDescriptor(name="only:8b", backend="fake", parameters_b=8, capabilities={"completion"}))]
    assign_tiers(profs, {})
    assert profs[0].tier in (Tier.FAST, Tier.STANDARD, Tier.DEEP)
    two = [
        ModelProfile.from_descriptor(ModelDescriptor(name="a:8b", backend="fake", parameters_b=8, capabilities={"completion"})),
        ModelProfile.from_descriptor(ModelDescriptor(name="b:14b", backend="fake", parameters_b=14, capabilities={"completion"})),
    ]
    assign_tiers(two, {})
    assert {p.tier for p in two} >= {Tier.FAST, Tier.DEEP}
    assign_tiers(two, {Tier.DEEP: "a:8b"})
    assert next(p for p in two if p.name == "a:8b").tier == Tier.DEEP


async def test_router_prefers_specialty_and_resident(tmp_path):
    sched, backend, profiles = make_scheduler(tmp_path)
    await sched.refresh_models()
    router = ModelRouter(profiles)
    c = router.candidates(ModelRequest(capability="coding", tier=Tier.STANDARD), loaded=set(), headroom=100 * GB, num_ctx=8192)
    assert c[0].profile.name == "medium-coder:32b"
    c = router.candidates(ModelRequest(capability="writing", tier=Tier.STANDARD), loaded={"medium:14b"}, headroom=100 * GB, num_ctx=8192)
    assert c[0].profile.name == "medium:14b"
    c = router.candidates(ModelRequest(capability="writing", tier=Tier.STANDARD, exclude={"medium:14b"}), loaded=set(), headroom=100 * GB, num_ctx=8192)
    assert c[0].profile.name != "medium:14b"


async def test_router_learns_from_failures(tmp_path):
    sched, backend, profiles = make_scheduler(tmp_path)
    await sched.refresh_models()
    router = ModelRouter(profiles)
    bad = profiles.get("medium-coder:32b")
    for _ in range(6):
        bad.record_call("coding", ok=False, duration_s=1.0)
    profiles.save(bad)
    c = router.candidates(ModelRequest(capability="coding", tier=Tier.STANDARD), loaded=set(), headroom=100 * GB, num_ctx=8192)
    assert c[0].profile.name == "medium:14b"


async def test_router_time_budget_penalises_slow_models(tmp_path):
    sched, backend, profiles = make_scheduler(tmp_path)
    await sched.refresh_models()
    router = ModelRouter(profiles)
    big = profiles.get("large:70b")
    big.tokens_per_s = 2.0
    profiles.save(big)
    c = router.candidates(ModelRequest(capability="reasoning", tier=Tier.DEEP, time_budget_s=30, expected_completion_tokens=800),
                          loaded=set(), headroom=100 * GB, num_ctx=8192)
    assert any("budget" in r for r in c[0].reasons)


async def test_scheduler_shares_one_instance(tmp_path):
    sched, backend, _ = make_scheduler(tmp_path, parallel=3)
    await sched.start()
    req = ModelRequest(capability="research", tier=Tier.FAST)

    async def worker():
        async with await sched.acquire(req) as lease:
            return await lease.chat([ChatMessage("user", "hi")])

    results = await asyncio.gather(*(worker() for _ in range(6)))
    assert len(results) == 6
    assert backend.load_events.count(("load", "small:7b")) == 1
    assert backend.max_active["small:7b"] <= 3
    await sched.stop()


async def test_scheduler_pinned_model_and_metrics(tmp_path):
    sched, backend, profiles = make_scheduler(tmp_path)
    await sched.start()
    async with await sched.acquire(ModelRequest(capability="writing", model="medium:14b")) as lease:
        await lease.chat([ChatMessage("user", "write")])
    p = profiles.get("medium:14b")
    assert p.calls == 1 and p.per_capability["writing"].successes == 1
    assert p.observed_memory is not None  # learned from backend.loaded()
    await sched.stop()


async def test_scheduler_evicts_idle_to_fit(tmp_path):
    # 100 GB total, 70% ceiling -> 70 GB target. coder (24 GB) + 14b (11.5 GB) fit together;
    # the 70b (49 GB) only fits once the least recently used idle instance is evicted.
    sched, backend, _ = make_scheduler(tmp_path, total_gb=100, available_gb=80, parallel=1)
    await sched.start()
    async with await sched.acquire(ModelRequest(capability="x", model="medium-coder:32b")) as lease:
        await lease.chat([ChatMessage("user", "a")])
    async with await sched.acquire(ModelRequest(capability="x", model="medium:14b")) as lease:
        await lease.chat([ChatMessage("user", "b")])
    assert set(sched.instances) == {"medium-coder:32b", "medium:14b"}
    async with await sched.acquire(ModelRequest(capability="x", model="large:70b"), timeout_s=5) as lease:
        pass
    assert set(sched.instances) == {"medium:14b", "large:70b"}
    assert sched.stats["evictions"] == 1
    assert sched.budget.allocated <= sched.budget.view(sched.monitor.latest).usable
    await sched.stop()


async def test_scheduler_fails_fast_when_model_can_never_fit(tmp_path):
    sched, backend, _ = make_scheduler(tmp_path, total_gb=60, available_gb=20, parallel=1)
    await sched.start()
    from swarm.models.backend import BackendError

    with pytest.raises(BackendError, match="memory ceiling"):
        await sched.acquire(ModelRequest(capability="x", model="large:70b"), timeout_s=0.3)
    await sched.stop()


async def test_scheduler_waits_when_busy_instance_will_free_up(tmp_path):
    # One slot per instance: the second request must wait for the first call to finish, not load a copy.
    sched, backend, _ = make_scheduler(tmp_path, parallel=1)
    backend.models["small:7b"].latency_s = 0.2
    await sched.start()
    req = ModelRequest(capability="x", tier=Tier.FAST)

    async def worker():
        async with await sched.acquire(req, timeout_s=5) as lease:
            await lease.chat([ChatMessage("user", "hi")])

    await asyncio.gather(worker(), worker())
    assert backend.load_events.count(("load", "small:7b")) == 1 and backend.max_active["small:7b"] == 1
    await sched.stop()


async def test_scheduler_unloads_idle_instances(tmp_path):
    sched, backend, _ = make_scheduler(tmp_path)
    sched.idle_unload_s = 0.05
    await sched.start()
    async with await sched.acquire(ModelRequest(capability="x", tier=Tier.FAST)) as lease:
        await lease.chat([ChatMessage("user", "a")])
    await asyncio.sleep(0.3)
    assert "small:7b" not in sched.instances
    assert ("unload", "small:7b") in backend.load_events
    await sched.stop()


async def test_scheduler_records_failures(tmp_path):
    def responder(model, messages, tools, schema):
        raise RuntimeError("boom")

    sched, backend, profiles = make_scheduler(tmp_path, responder=responder)
    await sched.start()
    async with await sched.acquire(ModelRequest(capability="x", tier=Tier.FAST)) as lease:
        with pytest.raises(RuntimeError):
            await lease.chat([ChatMessage("user", "a")])
    assert profiles.get("small:7b").failures == 1
    assert sched.stats["failures"] == 1
    await sched.stop()


@requires_ollama()
async def test_ollama_backend_roundtrip():
    from swarm.models.ollama import OllamaBackend

    b = OllamaBackend(os.environ.get("SWARM_OLLAMA_HOST", "http://localhost:11434"), timeout_s=120)
    assert await b.health()
    models = [m for m in await b.list_models() if m.is_chat_model and "embedding" not in m.capabilities]
    assert models
    small = min(models, key=lambda m: m.parameters_b)
    r = await b.chat(small.name, [ChatMessage("user", "Reply with the single word: pong")], max_tokens=20, think=False if "thinking" in small.capabilities else None)
    assert "pong" in r.content.lower()
    assert r.completion_tokens > 0 and r.tokens_per_s > 0
    r2 = await b.chat(small.name, [ChatMessage("user", "Return JSON with key answer set to 42")],
                      json_schema={"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]},
                      think=False if "thinking" in small.capabilities else None)
    import json
    assert json.loads(r2.content)["answer"] == 42
    await b.close()


async def test_scheduler_falls_back_to_lower_tier_when_deep_cannot_fit(tmp_path):
    sched, backend, _ = make_scheduler(tmp_path, total_gb=60, available_gb=30, parallel=1)
    await sched.start()
    async with await sched.acquire(ModelRequest(capability="synthesis", tier=Tier.DEEP), timeout_s=2) as lease:
        assert lease.model != "large:70b"
    assert any(e["type"] == "model.tier_fallback" for e in sched.bus.history)
    await sched.stop()
