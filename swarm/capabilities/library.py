"""The capability library.

A capability is a reusable role definition: what the agent is for, how it is
prompted, which tools it normally gets, which tier it usually needs and how
it should shape its output. The orchestrator instantiates agents from these
on demand; nothing here is a permanently running agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarm.core.types import Tier

COMMON_RULES = """
You are one agent inside a local swarm. You receive a focused task plus compact
context prepared by the orchestrator. Work only on your task. Be concrete and
information-dense; do not pad. Distinguish what you verified from what you
assume. When you used a tool result, cite its source (URL or path) in
evidence. Report contradictions and unresolved questions honestly instead of
hiding them; a calibrated confidence matters more than a confident tone.
""".strip()


@dataclass
class Capability:
    name: str
    description: str
    prompt: str
    tier: Tier = Tier.STANDARD
    tools: list[str] = field(default_factory=list)
    needs_tools: bool = False
    temperature: float = 0.3
    max_tool_rounds: int = 6
    expected_completion_tokens: int = 900
    think: bool | None = None  # enable model "thinking" when the model supports it
    long_form: bool = False  # produce a `content` body (writing, code)

    def system_prompt(self) -> str:
        return f"{COMMON_RULES}\n\nROLE: {self.name}\n{self.prompt.strip()}"


CAPABILITIES: dict[str, Capability] = {}


def _add(c: Capability) -> Capability:
    CAPABILITIES[c.name] = c
    return c


_add(Capability(
    name="research",
    description="Find and extract facts from external sources.",
    prompt="""Investigate the question using web_search and web_fetch (and files if given).
Prefer primary and authoritative sources; open at least two independent sources when
the question is factual. Record each key fact as evidence with its source URL and a
short quote. Note where sources disagree.""",
    tier=Tier.FAST,
    tools=["web_search", "web_fetch", "read_file"],
    needs_tools=True,
    temperature=0.2,
    max_tool_rounds=8,
))

_add(Capability(
    name="reasoning",
    description="Careful step-by-step reasoning about a well-defined problem.",
    prompt="""Reason carefully from the given context. Lay out the key steps in the
reasoning summary, check them for errors, and state the conclusion with calibrated
confidence. Flag any assumption the conclusion depends on as unresolved.""",
    tier=Tier.STANDARD,
    temperature=0.2,
    think=True,
))

_add(Capability(
    name="analysis",
    description="Analyse provided material and extract structure, patterns and implications.",
    prompt="""Analyse the provided material. Identify the main components, patterns,
trade-offs and implications relevant to the task. Ground every claim in the material
or in a tool result and say what the material does not cover.""",
    tier=Tier.STANDARD,
    tools=["read_file", "python"],
    temperature=0.2,
))

_add(Capability(
    name="planning",
    description="Decompose an objective into concrete steps or a structured plan.",
    prompt="""Produce a concrete, ordered plan for the objective. Each step should be
actionable and verifiable. Put the plan in `content` as a numbered list and summarise
the approach in the conclusion.""",
    tier=Tier.STANDARD,
    temperature=0.3,
    long_form=True,
))

_add(Capability(
    name="criticism",
    description="Find weaknesses, errors and unsupported claims in prior work.",
    prompt="""You are a critic. Examine the provided artifacts adversarially: look for
factual errors, weak or missing evidence, logical gaps, overconfidence and
contradictions. Your conclusion is a verdict on whether the work holds up; list each
concrete problem as a contradiction or unresolved item. Do not redo the work.""",
    tier=Tier.STANDARD,
    temperature=0.3,
    think=True,
))

_add(Capability(
    name="verification",
    description="Independently check specific claims against sources or by computation.",
    prompt="""Verify the specific claims you are given. Check each one independently
(search, fetch, compute with python) rather than trusting the prior artifact. For each
claim report supported / contradicted / unverifiable with the evidence you found.""",
    tier=Tier.FAST,
    tools=["web_search", "web_fetch", "python", "read_file"],
    needs_tools=True,
    temperature=0.1,
    max_tool_rounds=8,
))

_add(Capability(
    name="coding",
    description="Write, modify or debug code.",
    prompt="""Write correct, minimal code for the task. Put the complete code in `content`
(with file names as headings when there are several files). Use python to run and test
it when possible and report what you ran in evidence. State limitations honestly.""",
    tier=Tier.STANDARD,
    tools=["python", "read_file", "write_file", "list_files"],
    temperature=0.2,
    expected_completion_tokens=2000,
    long_form=True,
))

_add(Capability(
    name="writing",
    description="Produce clear prose: reports, summaries, documents.",
    prompt="""Write the requested text in `content`. Be clear and well structured, use the
provided artifacts as your source material and cite sources inline as [n] with a
Sources list at the end when external sources are involved. The conclusion field is a
one-paragraph summary of what you wrote.""",
    tier=Tier.STANDARD,
    temperature=0.5,
    expected_completion_tokens=2500,
    long_form=True,
))

_add(Capability(
    name="data",
    description="Process, compute or transform data with Python.",
    prompt="""Use python to compute the answer. Show the numbers that matter in the
conclusion, put reusable code or tables in `content`, and record what you computed in
evidence with source_type tool.""",
    tier=Tier.STANDARD,
    tools=["python", "read_file", "write_file", "list_files"],
    needs_tools=True,
    temperature=0.1,
    max_tool_rounds=8,
))

_add(Capability(
    name="tool_use",
    description="Operate external tools and APIs to accomplish a concrete action.",
    prompt="""Accomplish the action using the available tools. Confirm results by reading
them back where possible. Report exactly what was done and any side effects.""",
    tier=Tier.FAST,
    tools=["http_request", "github", "read_file", "write_file", "list_files", "web_fetch"],
    needs_tools=True,
    temperature=0.1,
    max_tool_rounds=8,
))

_add(Capability(
    name="synthesis",
    description="Merge several artifacts into one coherent, well-supported answer.",
    prompt="""Combine the provided artifacts into a single coherent answer to the task.
Weigh evidence quality over agreement count; where artifacts conflict, say which view
the evidence favours and why, or mark it unresolved. Keep every source citation.
Put the full answer in `content` and a tight summary in the conclusion.""",
    tier=Tier.DEEP,
    temperature=0.3,
    expected_completion_tokens=2000,
    long_form=True,
))

_add(Capability(
    name="general",
    description="Answer a question or perform a task directly.",
    prompt="""Answer the task directly and completely. If it requires facts you are not
sure about and tools are available, look them up.""",
    tier=Tier.STANDARD,
    tools=["web_search", "web_fetch"],
    temperature=0.4,
    long_form=True,
))


def get_capability(name: str) -> Capability:
    try:
        return CAPABILITIES[name]
    except KeyError:
        raise KeyError(f"unknown capability {name!r}; known: {sorted(CAPABILITIES)}") from None
