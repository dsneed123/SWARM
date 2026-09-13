"""Hardware telemetry and settings: memory ceiling, tier pins, models."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from swarm.tui.format import dur, gb, pct, status
from swarm.tui.screens.dashboard import _refill
from swarm.tui.widgets.panels import HardwarePanel


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("plus", "ceiling(5)", "Ceiling +5%"),
        Binding("equals_sign", "ceiling(5)", "Ceiling +5%", show=False),
        Binding("minus", "ceiling(-5)", "Ceiling -5%"),
        Binding("m", "toggle_mode", "Default mode"),
        Binding("u", "unload", "Unload model"),
        Binding("r", "refresh_models", "Refresh models"),
        Binding("1", "pin('fast')", "Pin as FAST"),
        Binding("2", "pin('standard')", "Pin as STANDARD"),
        Binding("3", "pin('deep')", "Pin as DEEP"),
        Binding("0", "unpin", "Clear pins"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("hardware & settings", classes="title")
        yield HardwarePanel(id="hw")
        with Horizontal(classes="row"):
            with Vertical(classes="half panel"):
                yield Static("Telemetry & scheduling", classes="panel-title")
                yield Static(id="telemetry")
            with Vertical(classes="half panel"):
                yield Static("Models (u unload · 1/2/3 pin tier · 0 clear pins)", classes="panel-title")
                yield DataTable(id="models", cursor_type="row")
        yield Static(id="allocs", classes="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#models", DataTable).add_columns("model", "tier", "params", "ctx", "state", "memory", "tok/s", "calls", "fail", "tools", "think")
        self.query_one("#models", DataTable).focus()
        if self.app.snapshot:
            self.update_from(self.app.snapshot)

    def update_from(self, snap: dict[str, Any]) -> None:
        self.query_one("#hw", HardwarePanel).update_from(snap)
        hw = snap.get("hardware") or {}
        info = snap.get("hardware_info") or {}
        sched = snap.get("scheduler") or {}
        b = sched.get("budget") or {}
        st = sched.get("stats") or {}
        settings = snap.get("settings") or {}
        orch = snap.get("orchestrator") or {}
        mem = snap.get("memory") or {}
        lines = [
            f"[b]{escape(str(info.get('hostname', '')))}[/]  {escape(str(info.get('gpu_name') or 'no GPU'))}  {info.get('cpu_count')} CPUs  {info.get('arch')}  "
            f"{'unified memory' if info.get('unified_memory') else ''}",
            f"total {gb(hw.get('mem_total'))}  used {gb(hw.get('mem_used'))} ({hw.get('mem_used_percent')}%)  available {gb(hw.get('mem_available'))}  swap {gb(hw.get('swap_used'))}",
            f"GPU {pct(hw.get('gpu_util'))}  temp {hw.get('gpu_temp_c') or '-'}°C  power {hw.get('gpu_power_w') or '-'} W    CPU {pct(hw.get('cpu_percent'))}  load {hw.get('load1', 0):.2f}",
            "",
            f"[b]Swarm ceiling[/] {settings.get('memory_ceiling_percent', 0):.0f}% of total  (+/- adjusts)",
            f"  target {gb(b.get('target'))}   usable now {gb(b.get('usable'))}   allocated {gb(b.get('allocated'))}   headroom {gb(b.get('headroom'))}   reserve {gb(b.get('reserve'))}",
            "",
            f"[b]Scheduler[/] {len(sched.get('instances') or [])} instances · {sched.get('active', 0)} inferring / cap {sched.get('inference_cap', 0)} · {sched.get('waiting', 0)} waiting for a model",
            f"  calls {st.get('calls', 0)}  failures {st.get('failures', 0)}  loads {st.get('loads', 0)}  unloads {st.get('unloads', 0)}  evictions {st.get('evictions', 0)}",
            f"[b]Orchestrator[/] tier {orch.get('tier')}  model {escape(str(orch.get('model') or 'router'))}  calls {orch.get('calls', 0)}  failures {orch.get('failures', 0)}",
            f"[b]Default mode[/] {settings.get('default_mode')}  (m toggles)    [b]Tier pins[/] {escape(str({k: v for k, v in (settings.get('tiers') or {}).items() if v}) or 'none')}",
            f"[b]Memory[/] {mem.get('facts', 0)} facts · {mem.get('artifacts', 0)} artifacts · {mem.get('failures', 0)} failure records",
            f"[b]Backend[/] {settings.get('backend')} @ {escape(str(settings.get('ollama_host')))}   workspace {escape(str(settings.get('workspace')))}",
        ]
        self.query_one("#telemetry", Static).update("\n".join(lines))
        inst = {i["name"]: i for i in sched.get("instances") or []}
        rows = []
        for m in sched.get("models") or []:
            i = inst.get(m["name"])
            pins = settings.get("tiers") or {}
            pinned = "*" if m["name"] in pins.values() else ""
            rows.append((
                m["name"] + (" [dim](missing)[/]" if m.get("disabled") else ""), m["tier"] + pinned, f"{m.get('params_b', 0):.0f}B",
                str(m.get("ctx") or "-"), status(i["state"]) if i else "-",
                (gb(i["memory"]) + ("" if i["observed"] else " est")) if i else (gb(m["observed_mem"]) + " obs" if m.get("observed_mem") else "-"),
                f"{m.get('tps', 0):.0f}" if m.get("tps") else "-", str(m.get("calls", 0)), str(m.get("failures", 0)),
                "y" if m.get("tools") else "", "y" if m.get("thinking") else "",
            ))
        _refill(self.query_one("#models", DataTable), rows, 0)
        allocs = b.get("allocations") or []
        self.query_one("#allocs", Static).update(
            "[b]Allocations[/] " + (", ".join(f"{a['owner']} {gb(a['bytes'])}{'' if a['observed'] else '~'}" for a in allocs) if allocs else "none")
            + "\n[dim]~ = estimated, replaced by observed memory once the backend reports it. Idle instances unload after "
            + dur(900) + " or sooner under budget pressure.[/]"
        )

    def _selected_model(self) -> str | None:
        t = self.query_one("#models", DataTable)
        if not t.row_count:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value).split(" ")[0]
        except Exception:  # noqa: BLE001
            return None

    async def action_ceiling(self, delta: int) -> None:
        cur = float((self.app.snapshot.get("settings") or {}).get("memory_ceiling_percent") or 70)
        await self.app.call("settings", changes={"memory_ceiling_percent": max(10, min(95, cur + delta))})
        await self.app.refresh_snapshot()

    async def action_toggle_mode(self) -> None:
        cur = (self.app.snapshot.get("settings") or {}).get("default_mode", "autonomous")
        await self.app.call("settings", changes={"default_mode": "interactive" if cur == "autonomous" else "autonomous"})
        await self.app.refresh_snapshot()

    async def action_unload(self) -> None:
        name = self._selected_model()
        if name:
            ok = await self.app.call("unload_model", name=name)
            self.notify(f"unloaded {name}" if ok else f"{name} is not idle or not loaded")

    async def action_refresh_models(self) -> None:
        n = await self.app.call("refresh_models")
        self.notify(f"{n} models known")

    async def action_pin(self, tier: str) -> None:
        name = self._selected_model()
        if name:
            await self.app.call("settings", changes={f"tier_{tier}": name})
            await self.app.call("refresh_models")
            await self.app.refresh_snapshot()

    async def action_unpin(self) -> None:
        await self.app.call("settings", changes={"tier_fast": None, "tier_standard": None, "tier_deep": None})
        await self.app.call("refresh_models")
        await self.app.refresh_snapshot()
