"""Model scheduler: loads, shares, reuses and unloads model instances.

Agents never talk to a backend directly. They ask the scheduler for a lease
on a model that matches a ``ModelRequest``; the scheduler picks a model via
the router, makes sure it is resident within the memory budget (evicting
idle instances if it has to), hands out a concurrency slot, and records
what it observes (memory, throughput, latency, failures) into the profile.

One loaded model instance serves many agents concurrently; a second copy is
never loaded. When nothing fits, the request waits until a slot or memory
frees up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from swarm.config import Settings
from swarm.core.events import EventBus
from swarm.core.types import Tier
from swarm.hardware.budget import ResourceBudget
from swarm.hardware.telemetry import HardwareMonitor
from swarm.models.backend import BackendError, ChatMessage, ChatResult, ModelBackend, ToolSpec
from swarm.models.profiles import ModelProfile, ProfileStore
from swarm.models.router import Candidate, ModelRequest, ModelRouter

log = logging.getLogger(__name__)


@dataclass
class ModelInstance:
    name: str
    num_ctx: int
    parallel: int
    state: str = "loading"  # loading | ready | unloading
    loaded_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    active: int = 0
    total_calls: int = 0
    estimated_mem: int = 0
    observed_mem: int | None = None
    external: bool = False  # loaded by something other than the swarm
    leases: int = 0  # agents currently holding a lease (may be idle between calls)

    @property
    def owner(self) -> str:
        return f"model:{self.name}"

    @property
    def memory(self) -> int:
        return self.observed_mem or self.estimated_mem

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "num_ctx": self.num_ctx,
            "state": self.state,
            "active": self.active,
            "leases": self.leases,
            "total_calls": self.total_calls,
            "memory": self.memory,
            "observed": self.observed_mem is not None,
            "idle_s": round(time.time() - self.last_used, 1),
            "external": self.external,
        }


class Lease:
    """A right to run inference on an instance. Use as an async context manager."""

    def __init__(self, scheduler: ModelScheduler, instance: ModelInstance, profile: ModelProfile,
                 request: ModelRequest, candidate: Candidate) -> None:
        self.scheduler = scheduler
        self.instance = instance
        self.profile = profile
        self.request = request
        self.candidate = candidate
        self.released = False

    @property
    def model(self) -> str:
        return self.instance.name

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool | None = None,
    ) -> ChatResult:
        return await self.scheduler._run(self, messages, tools, json_schema, temperature, max_tokens, think)

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.scheduler._release(self)

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()


class ModelScheduler:
    def __init__(
        self,
        backend: ModelBackend,
        profiles: ProfileStore,
        budget: ResourceBudget,
        monitor: HardwareMonitor,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self.backend = backend
        self.profiles = profiles
        self.router = ModelRouter(profiles)
        self.budget = budget
        self.monitor = monitor
        self.bus = bus
        self.settings = settings
        self.instances: dict[str, ModelInstance] = {}
        self.default_ctx = 8192
        self.idle_unload_s = 900.0
        self.reconcile_interval_s = 5.0
        self._changed = asyncio.Condition()
        self._load_locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self.waiting = 0
        self.stats = {"calls": 0, "failures": 0, "loads": 0, "unloads": 0, "evictions": 0}

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        await self.refresh_models()
        await self._reconcile()
        self._task = asyncio.create_task(self._reconcile_loop(), name="model-scheduler")

    async def stop(self, unload: bool = False) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if unload:
            for name in list(self.instances):
                if not self.instances[name].external:
                    await self._unload(name)

    async def refresh_models(self) -> list[ModelProfile]:
        descriptors = await self.backend.list_models()
        pins = {t: self.settings.tiers.pinned(t) for t in Tier}
        profiles = self.profiles.sync(descriptors, pins)
        self.bus.publish("models.refreshed", count=len(profiles))
        return profiles

    # --- admission ------------------------------------------------------

    @property
    def parallel(self) -> int:
        return max(1, self.settings.ollama.num_parallel)

    def _num_ctx_for(self, req: ModelRequest, profile: ModelProfile) -> int:
        ctx = max(self.default_ctx, req.min_context)
        ctx = ((ctx + 4095) // 4096) * 4096
        if profile.context_length:
            ctx = min(ctx, profile.context_length)
        return ctx

    def _inference_cap(self) -> int:
        cap = self.settings.hardware.max_concurrent_inference
        if cap > 0:
            return cap
        return max(1, len([i for i in self.instances.values() if i.state == "ready"])) * self.parallel + self.parallel

    def _active_total(self) -> int:
        return sum(i.active for i in self.instances.values())

    def _loaded_names(self) -> set[str]:
        return {n for n, i in self.instances.items() if i.state == "ready"}

    async def acquire(self, req: ModelRequest, timeout_s: float | None = None) -> Lease:
        """Block until a suitable model instance with a free slot is available."""
        deadline = time.monotonic() + (timeout_s or self.settings.ollama.request_timeout_s)
        self.waiting += 1
        try:
            while True:
                lease = await self._try_acquire(req)
                if lease is not None:
                    return lease
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no model available for {req.capability}/{req.tier.value}")
                async with self._changed:
                    try:
                        await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 2.0))
                    except TimeoutError:
                        pass
        finally:
            self.waiting -= 1

    async def _try_acquire(self, req: ModelRequest) -> Lease | None:
        sample = self.monitor.latest
        headroom = self.budget.view(sample).headroom
        loaded = self._loaded_names()
        cands = self.router.candidates(
            req, loaded=loaded, headroom=headroom, num_ctx=req.min_context or self.default_ctx,
            parallel=self.parallel,
        )
        if not cands:
            raise BackendError(f"no installed model can serve {req.capability}/{req.tier.value}"
                               + (f" (pinned {req.model})" if req.model else ""))
        # Fail fast when no candidate could ever fit under the ceiling (as opposed to
        # not fitting right now); waiting would never help.
        usable = self.budget.view(sample).usable

        def possible(cs: list[Candidate]) -> list[Candidate]:
            return [c for c in cs if c.loaded or c.profile.estimate_memory(self._num_ctx_for(req, c.profile), self.parallel) <= usable]

        fitting = possible(cands)
        if not fitting and not req.model:
            # Degrade to a lower tier that fits rather than fail: the task still gets
            # the best model this machine can hold right now.
            tier = req.tier
            while not fitting and tier != tier.down():
                tier = tier.down()
                lower = self.router.candidates(replace(req, tier=tier), loaded=loaded, headroom=headroom,
                                               num_ctx=req.min_context or self.default_ctx, parallel=self.parallel)
                fitting = possible(lower)
            if fitting:
                self.bus.publish("model.tier_fallback", requested=req.tier.value, used=tier.value,
                                 capability=req.capability, usable_gb=round(usable / 1024**3, 1))
        if not fitting:
            need = min(c.profile.estimate_memory(self._num_ctx_for(req, c.profile), self.parallel) for c in cands)
            raise BackendError(
                f"no model for {req.capability}/{req.tier.value} fits within the memory ceiling "
                f"(needs ~{need / 1024**3:.0f} GB, usable {usable / 1024**3:.0f} GB); raise the ceiling or free memory")
        cands = fitting
        # First pass: a resident candidate with a free slot and no big score gap.
        best = cands[0].score
        for c in cands:
            inst = self.instances.get(c.profile.name)
            if inst and inst.state == "ready" and inst.active < inst.parallel and c.score >= best - 0.6:
                if self._active_total() < self._inference_cap():
                    return self._grant(inst, c, req)
        # Second pass: load the best non-resident candidate that fits (evicting idle ones).
        for c in cands:
            if c.score < best - 1.0:
                break
            inst = self.instances.get(c.profile.name)
            if inst is not None:
                continue  # resident but busy; wait for a slot rather than loading more
            num_ctx = self._num_ctx_for(req, c.profile)
            need = c.profile.estimate_memory(num_ctx, self.parallel)
            if await self._make_room(need, protect=set()):
                inst = await self._load(c.profile, num_ctx)
                if inst is not None:
                    return self._grant(inst, c, req)
        return None

    def _grant(self, inst: ModelInstance, cand: Candidate, req: ModelRequest) -> Lease:
        inst.leases += 1
        inst.last_used = time.time()
        lease = Lease(self, inst, cand.profile, req, cand)
        self.bus.publish("model.lease", model=inst.name, capability=req.capability,
                         tier=req.tier.value, reasons=cand.reasons, purpose=req.purpose)
        return lease

    def _release(self, lease: Lease) -> None:
        lease.instance.leases = max(0, lease.instance.leases - 1)
        lease.instance.last_used = time.time()
        self._notify()

    def _notify(self) -> None:
        async def _n() -> None:
            async with self._changed:
                self._changed.notify_all()

        try:
            asyncio.get_running_loop().create_task(_n())
        except RuntimeError:
            pass

    # --- loading / eviction --------------------------------------------

    async def _make_room(self, need: int, protect: set[str]) -> bool:
        sample = self.monitor.latest
        if self.budget.view(sample).headroom >= need:
            return True
        idle = sorted(
            (i for i in self.instances.values()
             if i.state == "ready" and i.active == 0 and i.leases == 0 and not i.external
             and i.name not in protect),
            key=lambda i: i.last_used,
        )
        for inst in idle:
            await self._unload(inst.name, reason="eviction")
            self.stats["evictions"] += 1
            if self.budget.view(self.monitor.latest).headroom >= need:
                return True
        return self.budget.view(self.monitor.latest).headroom >= need

    async def _load(self, profile: ModelProfile, num_ctx: int) -> ModelInstance | None:
        lock = self._load_locks.setdefault(profile.name, asyncio.Lock())
        async with lock:
            if profile.name in self.instances:
                return self.instances[profile.name]
            est = profile.estimate_memory(num_ctx, self.parallel)
            inst = ModelInstance(name=profile.name, num_ctx=num_ctx, parallel=self.parallel, estimated_mem=est)
            self.instances[profile.name] = inst
            self.budget.reserve_bytes(inst.owner, est, note=f"ctx {num_ctx}")
            self.bus.publish("model.loading", model=profile.name, num_ctx=num_ctx, estimated_mem=est)
            t0 = time.monotonic()
            try:
                await self.backend.load(profile.name, num_ctx, self.settings.ollama.keep_alive)
            except BackendError as e:
                log.warning("load failed for %s: %s", profile.name, e)
                self.instances.pop(profile.name, None)
                self.budget.release(inst.owner)
                self.bus.publish("model.load_failed", model=profile.name, error=str(e))
                profile.failures += 1
                self.profiles.save(profile)
                return None
            load_s = time.monotonic() - t0
            inst.state = "ready"
            self.stats["loads"] += 1
            if load_s > 0.5:
                profile.load_time_s = load_s if not profile.load_time_s else 0.7 * profile.load_time_s + 0.3 * load_s
                self.profiles.save(profile)
            self.bus.publish("model.loaded", model=profile.name, load_s=round(load_s, 1))
            await self._observe_memory()
            self._notify()
            return inst

    async def _unload(self, name: str, reason: str = "idle") -> None:
        inst = self.instances.get(name)
        if inst is None:
            return
        inst.state = "unloading"
        try:
            await self.backend.unload(name)
        except BackendError as e:
            log.warning("unload failed for %s: %s", name, e)
        self.instances.pop(name, None)
        self.budget.release(inst.owner)
        self.stats["unloads"] += 1
        self.bus.publish("model.unloaded", model=name, reason=reason)
        self._notify()

    async def unload_model(self, name: str) -> bool:
        inst = self.instances.get(name)
        if inst is None or inst.active > 0:
            return False
        await self._unload(name, reason="manual")
        return True

    async def _observe_memory(self) -> None:
        try:
            loaded = await self.backend.loaded()
        except BackendError as e:
            log.debug("loaded() failed: %s", e)
            return
        seen: set[str] = set()
        for lm in loaded:
            seen.add(lm.name)
            inst = self.instances.get(lm.name)
            if inst is None:
                inst = ModelInstance(name=lm.name, num_ctx=lm.context_length or self.default_ctx,
                                     parallel=self.parallel, state="ready", external=True,
                                     estimated_mem=lm.size_bytes)
                self.instances[lm.name] = inst
                self.bus.publish("model.external", model=lm.name, memory=lm.size_bytes)
            if lm.size_bytes > 0:
                inst.observed_mem = lm.size_bytes
                if not inst.external:
                    self.budget.observe(inst.owner, lm.size_bytes)
                    prof = self.profiles.get(lm.name)
                    if prof:
                        prof.observe_memory(lm.size_bytes, inst.num_ctx)
                        self.profiles.save(prof)
        for name, inst in list(self.instances.items()):
            if name not in seen and inst.state == "ready" and inst.active == 0:
                # The backend dropped it (keep-alive expiry, external unload, restart).
                self.instances.pop(name, None)
                if not inst.external:
                    self.budget.release(inst.owner)
                self.bus.publish("model.unloaded", model=name, reason="backend")
                self._notify()

    async def _reconcile(self) -> None:
        await self._observe_memory()
        now = time.time()
        over = self.budget.utilisation(self.monitor.latest) > 1.0
        for inst in list(self.instances.values()):
            if inst.external or inst.state != "ready" or inst.active or inst.leases:
                continue
            idle = now - inst.last_used
            if idle > self.idle_unload_s or (over and idle > 30):
                await self._unload(inst.name, reason="over budget" if over else "idle")

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval_s)
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001
                log.exception("scheduler reconcile failed")

    # --- inference ------------------------------------------------------

    async def _run(
        self,
        lease: Lease,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        json_schema: dict[str, Any] | None,
        temperature: float | None,
        max_tokens: int | None,
        think: bool | None,
    ) -> ChatResult:
        inst = lease.instance
        # Wait for a slot on this instance (agents sharing a model queue here).
        while inst.active >= inst.parallel or self._active_total() >= self._inference_cap():
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=2.0)
                except TimeoutError:
                    pass
        inst.active += 1
        inst.total_calls += 1
        inst.last_used = time.time()
        self.stats["calls"] += 1
        t0 = time.monotonic()
        ok = True
        try:
            result = await self.backend.chat(
                inst.name,
                messages,
                tools=tools,
                json_schema=json_schema,
                num_ctx=inst.num_ctx,
                temperature=temperature,
                max_tokens=max_tokens,
                keep_alive=self.settings.ollama.keep_alive,
                think=think,
            )
        except Exception:
            ok = False
            self.stats["failures"] += 1
            raise
        finally:
            duration = time.monotonic() - t0
            inst.active -= 1
            inst.last_used = time.time()
            self._notify()
            prof = lease.profile
            if ok:
                r = result
                prof.record_call(
                    lease.request.capability, ok=True, duration_s=duration,
                    tokens_per_s=r.tokens_per_s, prompt_tokens_per_s=r.prompt_tokens_per_s,
                    load_time_s=r.load_duration_s, ts=time.time(),
                )
            else:
                prof.record_call(lease.request.capability, ok=False, duration_s=duration, ts=time.time())
            self.profiles.save(prof)
            self.bus.publish("model.call", model=inst.name, ok=ok, duration_s=round(duration, 2),
                             capability=lease.request.capability)
        return result

    # --- views ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        sample = self.monitor.latest
        return {
            "instances": [i.as_dict() for i in self.instances.values()],
            "budget": self.budget.view(sample).as_dict(),
            "waiting": self.waiting,
            "active": self._active_total(),
            "inference_cap": self._inference_cap(),
            "stats": dict(self.stats),
            "models": [
                {
                    "name": p.name,
                    "tier": p.tier.value,
                    "params_b": p.parameters_b,
                    "size": p.size_bytes,
                    "ctx": p.context_length,
                    "tools": p.supports_tools(),
                    "thinking": p.supports_thinking(),
                    "tps": round(p.tokens_per_s or 0, 1),
                    "calls": p.calls,
                    "failures": p.failures,
                    "observed_mem": p.observed_memory,
                    "disabled": p.disabled,
                    "specialties": p.specialties,
                }
                for p in sorted(self.profiles.all(), key=lambda p: p.parameters_b)
                if p.is_chat_model
            ],
        }
