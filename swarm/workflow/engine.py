"""DAG execution engine.

The engine only schedules: it finds ready nodes, starts them when the
orchestrator says hardware allows, tracks running node tasks, honours pause
and cancel, and picks up nodes that were added while running. What running a
node *means* (redundant agents, consensus, recovery) is the orchestrator's
``run_node`` callback, which sets the node's final state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from swarm.core.events import EventBus
from swarm.core.types import NodeStatus
from swarm.workflow.dag import DAG, NodeSpec

log = logging.getLogger(__name__)

RunNode = Callable[[NodeSpec], Awaitable[None]]


class ExecutionControl:
    def __init__(self) -> None:
        self.paused = asyncio.Event()
        self.paused.clear()
        self.cancelled = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()

    def pause(self) -> None:
        self.paused.set()
        self.resume.clear()

    def unpause(self) -> None:
        self.paused.clear()
        self.resume.set()

    def cancel(self) -> None:
        self.cancelled.set()
        self.resume.set()

    @property
    def is_paused(self) -> bool:
        return self.paused.is_set()


class Engine:
    def __init__(self, bus: EventBus, tick_s: float = 0.25) -> None:
        self.bus = bus
        self.tick_s = tick_s

    async def execute(
        self,
        dag: DAG,
        run_node: RunNode,
        *,
        task_id: str,
        control: ExecutionControl,
        can_start: Callable[[NodeSpec], bool] = lambda n: True,
    ) -> bool:
        running: dict[str, asyncio.Task] = {}
        try:
            while True:
                if control.cancelled.is_set():
                    for t in running.values():
                        t.cancel()
                    if running:
                        await asyncio.gather(*running.values(), return_exceptions=True)
                    for nid, st in dag.states.items():
                        if not st.status.terminal:
                            st.status = NodeStatus.CANCELLED
                            st.finished_at = time.time()
                    self.bus.publish("task.cancelled", task_id=task_id)
                    return False
                if control.is_paused and not running:
                    self.bus.publish("task.paused", task_id=task_id)
                    await control.resume.wait()
                    continue
                if not control.is_paused:
                    for node in dag.ready():
                        if node.id in running or not can_start(node):
                            continue
                        st = dag.states[node.id]
                        st.status = NodeStatus.RUNNING
                        st.started_at = st.started_at or time.time()
                        running[node.id] = asyncio.create_task(self._guard(dag, node, run_node, task_id), name=f"node-{task_id}-{node.id}")
                        self.bus.publish("node.started", task_id=task_id, node_id=node.id, capability=node.capability)
                for nid in dag.blocked():
                    st = dag.states[nid]
                    if not st.status.terminal:
                        st.status = NodeStatus.SKIPPED
                        st.finished_at = time.time()
                        st.error = "a dependency failed"
                        self.bus.publish("node.skipped", task_id=task_id, node_id=nid, reason="dependency failed")
                if not running and dag.is_done():
                    return dag.succeeded()
                if not running and not dag.ready():
                    # Nothing can run: remaining nodes are blocked or waiting on hardware.
                    if all(s.status.terminal for s in dag.states.values()):
                        return dag.succeeded()
                    await asyncio.sleep(self.tick_s)
                    continue
                if running:
                    done, _ = await asyncio.wait(running.values(), timeout=self.tick_s, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        for nid, task in list(running.items()):
                            if task is t:
                                running.pop(nid)
                else:
                    await asyncio.sleep(self.tick_s)
        finally:
            for t in running.values():
                t.cancel()

    async def _guard(self, dag: DAG, node: NodeSpec, run_node: RunNode, task_id: str) -> None:
        st = dag.states[node.id]
        try:
            await run_node(node)
        except asyncio.CancelledError:
            st.status = NodeStatus.CANCELLED
            raise
        except Exception as e:  # noqa: BLE001 - orchestrator bugs must not hang the task
            log.exception("run_node crashed for %s", node.id)
            st.status = NodeStatus.FAILED
            st.error = f"{type(e).__name__}: {e}"
            st.error_kind = "internal"
        finally:
            if not st.status.terminal:
                st.status = NodeStatus.FAILED
                st.error = st.error or "node runner returned without a terminal status"
            st.finished_at = time.time()
            self.bus.publish("node.finished", task_id=task_id, node_id=node.id, status=st.status.value,
                             error=st.error, elapsed_s=round(st.elapsed_s, 1))
