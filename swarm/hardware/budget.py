"""Memory budget accounting for the swarm.

The user picks a ceiling as a percentage of total unified memory (say 70%).
That is the upper operating boundary for everything the swarm loads. The
budget also respects reality: if other processes already hold memory, the
usable budget shrinks to what is actually available minus a safety reserve.

Allocations are tracked in a ledger keyed by an owner (normally a loaded
model instance). Estimates get replaced by observed measurements as soon as
the scheduler sees real numbers from the backend, so the ledger converges on
the truth instead of theoretical model sizes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.hardware.telemetry import HardwareSample

GB = 1024**3


@dataclass
class Allocation:
    owner: str
    estimated: int
    observed: int | None = None
    note: str = ""

    @property
    def bytes(self) -> int:
        return self.observed if self.observed is not None else self.estimated


@dataclass
class BudgetView:
    total: int
    ceiling_percent: float
    target: int  # ceiling_percent * total
    usable: int  # min(target, allocated + available - reserve)
    allocated: int  # sum of ledger
    headroom: int  # usable - allocated
    system_available: int
    reserve: int
    allocations: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "ceiling_percent": self.ceiling_percent,
            "target": self.target,
            "usable": self.usable,
            "allocated": self.allocated,
            "headroom": self.headroom,
            "system_available": self.system_available,
            "reserve": self.reserve,
            "allocations": self.allocations,
        }


class ResourceBudget:
    def __init__(self, ceiling_percent: float = 70.0, safety_reserve_gb: float = 4.0) -> None:
        self.ceiling_percent = ceiling_percent
        self.reserve = int(safety_reserve_gb * GB)
        self._ledger: dict[str, Allocation] = {}

    # --- ledger ---------------------------------------------------------

    def reserve_bytes(self, owner: str, estimated: int, note: str = "") -> Allocation:
        alloc = Allocation(owner=owner, estimated=estimated, note=note)
        self._ledger[owner] = alloc
        return alloc

    def observe(self, owner: str, observed: int) -> None:
        if owner in self._ledger:
            self._ledger[owner].observed = observed

    def release(self, owner: str) -> None:
        self._ledger.pop(owner, None)

    def allocation(self, owner: str) -> Allocation | None:
        return self._ledger.get(owner)

    @property
    def allocated(self) -> int:
        return sum(a.bytes for a in self._ledger.values())

    # --- views ----------------------------------------------------------

    def view(self, sample: HardwareSample) -> BudgetView:
        target = int(sample.mem_total * self.ceiling_percent / 100.0)
        # Memory the swarm could use right now: what it already holds plus what
        # the system still has free, keeping the reserve untouched.
        realistic = self.allocated + sample.mem_available - self.reserve
        usable = max(0, min(target, realistic))
        return BudgetView(
            total=sample.mem_total,
            ceiling_percent=self.ceiling_percent,
            target=target,
            usable=usable,
            allocated=self.allocated,
            headroom=max(0, usable - self.allocated),
            system_available=sample.mem_available,
            reserve=self.reserve,
            allocations=[
                {
                    "owner": a.owner,
                    "bytes": a.bytes,
                    "observed": a.observed is not None,
                    "note": a.note,
                }
                for a in self._ledger.values()
            ],
        )

    def fits(self, sample: HardwareSample, extra: int) -> bool:
        return self.view(sample).headroom >= extra

    def utilisation(self, sample: HardwareSample) -> float:
        """How full the swarm's target allocation is, in [0, 1+]."""
        v = self.view(sample)
        return v.allocated / v.target if v.target else 0.0
