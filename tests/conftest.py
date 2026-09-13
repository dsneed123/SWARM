from __future__ import annotations

import os
from pathlib import Path

import pytest

from swarm.artifacts.model import Artifact, Evidence, Provenance


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "ws"


def make_artifact(
    conclusion: str = "the sky is blue",
    confidence: float = 0.8,
    node_id: str = "n1",
    task_id: str = "task_1",
    evidence: list[Evidence] | None = None,
    **kw,
) -> Artifact:
    return Artifact(
        conclusion=conclusion,
        confidence=confidence,
        evidence=evidence or [],
        provenance=Provenance(task_id=task_id, node_id=node_id, **kw.pop("prov", {})),
        **kw,
    )


def requires_ollama() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        os.environ.get("SWARM_OLLAMA_TESTS") != "1", reason="set SWARM_OLLAMA_TESTS=1"
    )


def requires_hardware() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        os.environ.get("SWARM_HW_TESTS") != "1", reason="set SWARM_HW_TESTS=1 on the GX10"
    )
