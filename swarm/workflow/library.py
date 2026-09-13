"""Reusable user workflows stored as YAML in ``workspace/workflows``.

Bundled examples live in the repo's ``examples/workflows`` directory and are
listed alongside the user's own (user files shadow examples with the same
name).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from swarm.workflow.dag import WorkflowSpec

EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "workflows"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workflow"


class WorkflowLibrary:
    def __init__(self, user_dir: Path, examples_dir: Path | None = EXAMPLES_DIR) -> None:
        self.user_dir = user_dir
        self.examples_dir = examples_dir
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def _files(self) -> dict[str, Path]:
        files: dict[str, Path] = {}
        if self.examples_dir and self.examples_dir.exists():
            for p in sorted(self.examples_dir.glob("*.yaml")):
                files[p.stem] = p
        for p in sorted(self.user_dir.glob("*.yaml")):
            files[p.stem] = p
        return files

    def list(self) -> list[dict]:
        out = []
        for slug, path in self._files().items():
            try:
                spec = self.load(slug)
            except Exception as e:  # noqa: BLE001 - show broken files rather than hide them
                out.append({"slug": slug, "name": slug, "error": str(e), "path": str(path)})
                continue
            out.append({
                "slug": slug, "name": spec.name, "description": spec.description,
                "nodes": len(spec.nodes), "example": path.parent == self.examples_dir,
                "path": str(path), "objective_class": spec.objective_class,
            })
        return out

    def load(self, slug: str) -> WorkflowSpec:
        path = self._files().get(slug)
        if path is None:
            raise FileNotFoundError(f"no workflow named {slug!r}")
        data = yaml.safe_load(path.read_text()) or {}
        data.setdefault("immutable", True)
        return WorkflowSpec.model_validate(data)

    def save(self, spec: WorkflowSpec, slug: str | None = None) -> Path:
        slug = slug or _slug(spec.name)
        path = self.user_dir / f"{slug}.yaml"
        data = spec.model_dump(mode="json", exclude_defaults=True)
        data["name"] = spec.name
        data["nodes"] = [n.model_dump(mode="json", exclude_defaults=True) | {"id": n.id} for n in spec.nodes]
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        return path

    def delete(self, slug: str) -> bool:
        path = self.user_dir / f"{slug}.yaml"
        if path.exists():
            path.unlink()
            return True
        return False
