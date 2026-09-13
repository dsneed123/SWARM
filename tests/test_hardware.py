from __future__ import annotations

import time

from swarm.hardware.budget import GB, ResourceBudget
from swarm.hardware.telemetry import HardwareMonitor, HardwareSample
from tests.conftest import requires_hardware


def sample(total_gb=128, available_gb=100) -> HardwareSample:
    return HardwareSample(
        ts=time.time(),
        mem_total=total_gb * GB,
        mem_available=available_gb * GB,
        mem_used=(total_gb - available_gb) * GB,
        swap_used=0,
        cpu_percent=10.0,
        load1=1.0,
    )


def test_budget_target_is_ceiling_of_total():
    b = ResourceBudget(ceiling_percent=70, safety_reserve_gb=4)
    v = b.view(sample())
    assert v.target == int(128 * GB * 0.7)
    assert v.headroom == v.usable == v.target  # plenty available
    assert b.utilisation(sample()) == 0.0


def test_budget_respects_actual_availability():
    b = ResourceBudget(ceiling_percent=70, safety_reserve_gb=4)
    # Other processes hold most memory: only 20 GB available.
    v = b.view(sample(available_gb=20))
    assert v.usable == 16 * GB
    assert not b.fits(sample(available_gb=20), 17 * GB)
    assert b.fits(sample(available_gb=20), 16 * GB)


def test_budget_ledger_and_observation():
    b = ResourceBudget(ceiling_percent=50, safety_reserve_gb=0)
    b.reserve_bytes("model:a", 10 * GB)
    assert b.allocated == 10 * GB
    b.observe("model:a", 12 * GB)
    assert b.allocated == 12 * GB
    v = b.view(sample(available_gb=100))
    assert v.headroom == 64 * GB - 12 * GB
    b.release("model:a")
    assert b.allocated == 0


def test_monitor_samples_without_gpu():
    m = HardwareMonitor()
    s = m.sample()
    assert s.mem_total > 0 and s.mem_available > 0
    assert m.latest is s
    assert "cpu_percent" in m.average(10)


@requires_hardware()
def test_monitor_reads_gb10_gpu():
    m = HardwareMonitor()
    s = m.sample()
    assert s.gpu_name and "GB10" in s.gpu_name
    assert s.gpu_util is not None and s.gpu_temp_c is not None
    assert m.info.unified_memory
