"""Real-time hardware observation.

The GX10 (NVIDIA GB10) has unified memory: the GPU has no separate memory
pool and NVML reports "Not Supported" for memory queries. Memory is therefore
read from ``/proc/meminfo``; GPU utilisation, temperature and power come from
NVML when available. Everything degrades gracefully on machines without a GPU
so the rest of the system can be developed elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from collections import deque
from dataclasses import asdict, dataclass, field

import psutil

log = logging.getLogger(__name__)

try:  # optional dependency
    import pynvml  # type: ignore
except Exception:  # noqa: BLE001
    pynvml = None


@dataclass
class HardwareSample:
    ts: float
    mem_total: int
    mem_available: int
    mem_used: int
    swap_used: int
    cpu_percent: float
    load1: float
    gpu_util: float | None = None
    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    gpu_name: str | None = None

    @property
    def mem_used_percent(self) -> float:
        return 100.0 * self.mem_used / self.mem_total if self.mem_total else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["mem_used_percent"] = round(self.mem_used_percent, 1)
        return d


@dataclass
class HardwareInfo:
    hostname: str
    cpu_count: int
    mem_total: int
    gpu_name: str | None
    unified_memory: bool
    arch: str
    extras: dict = field(default_factory=dict)


def read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    out[key] = int(parts[0]) * 1024
    except FileNotFoundError:  # macOS / other dev machines
        vm = psutil.virtual_memory()
        out = {"MemTotal": vm.total, "MemAvailable": vm.available}
    return out


class _Nvml:
    def __init__(self) -> None:
        self.ok = False
        self.handle = None
        self.name: str | None = None
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(self.handle)
            self.name = name.decode() if isinstance(name, bytes) else str(name)
            self.ok = True
        except Exception as e:  # noqa: BLE001
            log.info("NVML unavailable: %s", e)

    def read(self) -> tuple[float | None, float | None, float | None]:
        if not self.ok:
            return None, None, None
        util = temp = power = None
        try:
            util = float(pynvml.nvmlDeviceGetUtilizationRates(self.handle).gpu)
        except Exception:  # noqa: BLE001
            pass
        try:
            temp = float(pynvml.nvmlDeviceGetTemperature(self.handle, 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(self.handle) / 1000.0
        except Exception:  # noqa: BLE001
            pass
        return util, temp, power


class HardwareMonitor:
    """Samples the machine on an interval and keeps a short history."""

    def __init__(self, interval_s: float = 1.0, history: int = 600) -> None:
        self.interval_s = interval_s
        self.history: deque[HardwareSample] = deque(maxlen=history)
        self._nvml = _Nvml()
        self._task: asyncio.Task | None = None
        self._latest: HardwareSample = self.sample()
        psutil.cpu_percent(None)  # prime

    @property
    def info(self) -> HardwareInfo:
        mi = read_meminfo()
        return HardwareInfo(
            hostname=platform.node(),
            cpu_count=os.cpu_count() or 1,
            mem_total=mi.get("MemTotal", 0),
            gpu_name=self._nvml.name,
            unified_memory=bool(self._nvml.name and "GB10" in self._nvml.name)
            or platform.machine() == "aarch64",
            arch=platform.machine(),
        )

    def sample(self) -> HardwareSample:
        mi = read_meminfo()
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", 0)
        swap_total = mi.get("SwapTotal", 0)
        swap_free = mi.get("SwapFree", 0)
        util, temp, power = self._nvml.read()
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0
        s = HardwareSample(
            ts=time.time(),
            mem_total=total,
            mem_available=avail,
            mem_used=max(0, total - avail),
            swap_used=max(0, swap_total - swap_free),
            cpu_percent=psutil.cpu_percent(None),
            load1=load1,
            gpu_util=util,
            gpu_temp_c=temp,
            gpu_power_w=power,
            gpu_name=self._nvml.name,
        )
        self._latest = s
        self.history.append(s)
        return s

    @property
    def latest(self) -> HardwareSample:
        return self._latest

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="hardware-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sample)
            except Exception:  # noqa: BLE001
                log.exception("hardware sample failed")
            await asyncio.sleep(self.interval_s)

    def average(self, seconds: float) -> dict[str, float]:
        cutoff = time.time() - seconds
        rows = [s for s in self.history if s.ts >= cutoff] or [self._latest]
        n = len(rows)
        gpu = [s.gpu_util for s in rows if s.gpu_util is not None]
        return {
            "cpu_percent": sum(s.cpu_percent for s in rows) / n,
            "gpu_util": (sum(gpu) / len(gpu)) if gpu else 0.0,
            "mem_used": sum(s.mem_used for s in rows) / n,
        }
