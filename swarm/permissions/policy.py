"""Tool permission policies.

Every tool has a category. A profile maps categories (and specific tools) to
Allow / Ask / Deny. Overrides layer on top: global user overrides, then the
workflow's overrides, then the node's. The most specific wins. Resolution is
pure and cheap; enforcement happens in the tool registry, which is the only
path from an agent to a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.core.types import Policy

# Category defaults per profile. Tool-specific entries take precedence over categories.
PROFILES: dict[str, dict[str, Policy]] = {
    "safe": {
        "*": Policy.DENY,
        "category:read": Policy.ALLOW,
        "category:network": Policy.ALLOW,
        "category:write": Policy.ASK,
        "category:execute": Policy.DENY,
        "category:external": Policy.DENY,
        "web_search": Policy.ALLOW,
        "web_fetch": Policy.ALLOW,
        "write_file": Policy.ASK,
        "python": Policy.ASK,
        "shell": Policy.DENY,
        "email": Policy.DENY,
    },
    "normal": {
        "*": Policy.ASK,
        "category:read": Policy.ALLOW,
        "category:network": Policy.ALLOW,
        "category:write": Policy.ALLOW,
        "category:execute": Policy.ASK,
        "category:external": Policy.ASK,
        "python": Policy.ALLOW,
        "shell": Policy.ASK,
        "github": Policy.ASK,
        "email": Policy.ASK,
    },
    "autonomous": {
        "*": Policy.ALLOW,
        "category:external": Policy.ALLOW,
        "shell": Policy.ALLOW,
        "email": Policy.ASK,
    },
}


@dataclass
class PermissionScope:
    """Where a tool call is happening, so overrides can be layered."""

    task_id: str | None = None
    workflow_overrides: dict[str, Policy] = field(default_factory=dict)
    node_overrides: dict[str, Policy] = field(default_factory=dict)
    node_allowed_tools: set[str] | None = None  # None = anything the profile allows


class PermissionResolver:
    def __init__(self, profile: str = "normal", overrides: dict[str, Policy] | None = None) -> None:
        self.set_profile(profile)
        self.overrides: dict[str, Policy] = dict(overrides or {})

    def set_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown permission profile {profile!r}; choose from {sorted(PROFILES)}")
        self.profile = profile

    def set_override(self, tool: str, policy: Policy | None) -> None:
        if policy is None:
            self.overrides.pop(tool, None)
        else:
            self.overrides[tool] = policy

    def resolve(self, tool: str, category: str, scope: PermissionScope | None = None) -> Policy:
        """Effective policy for one call.

        The user's global setting (override or profile) is the ceiling. A
        workflow may tighten it for its nodes, and a node may tighten it
        further; neither can loosen it, so a global Deny stays a Deny and a
        global Ask cannot be turned into Allow by a workflow file.
        """
        scope = scope or PermissionScope()
        if scope.node_allowed_tools is not None and tool not in scope.node_allowed_tools:
            return Policy.DENY
        result = self.overrides.get(tool) or self._profile_policy(tool, category)
        for layer in (scope.workflow_overrides, scope.node_overrides):
            if tool in layer:
                result = _tighten(layer[tool], result)
        return result

    def _profile_policy(self, tool: str, category: str) -> Policy:
        table = PROFILES[self.profile]
        if tool in table:
            return table[tool]
        if f"category:{category}" in table:
            return table[f"category:{category}"]
        return table.get("*", Policy.ASK)

    def table(self, tools: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
        """Effective policy per tool for display: (name, category) -> policy and source."""
        out = {}
        for name, category in tools:
            if name in self.overrides:
                out[name] = {"policy": self.overrides[name].value, "source": "override"}
            else:
                out[name] = {"policy": self._profile_policy(name, category).value, "source": self.profile}
        return out


_ORDER = {Policy.DENY: 0, Policy.ASK: 1, Policy.ALLOW: 2}


def _tighten(requested: Policy, ceiling: Policy) -> Policy:
    return requested if _ORDER[requested] <= _ORDER[ceiling] else ceiling
