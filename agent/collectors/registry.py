"""The collector registry: what the tick sweeps, and how each piece is diffed.

`agent/tick.py` used to gather a fixed set of state inline and project it
for diffing with hand-written per-field logic. That coupling is why the
wake-storm bug was so easy to reintroduce: adding a field to a collector
and adding it to the diff policy were two unrelated edits, and forgetting
the second one woke a fully autonomous model every 60 seconds.

Here a collector declares both halves together. `collect` produces the raw
state (which is what the model actually reads in its prompt), and
`projectors` declares how that state enters the *comparison* that decides
whether to wake the model at all. `policies` states, per key, which fields
are excluded entirely, which are banded with a deadband and at what step,
and which are compared exactly - so a test can enumerate a realistic
sample and assert nothing slipped in unaccounted for.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from agent.collectors.bands import band_with_deadband

# (raw value for this key, this key's previously *reported* projection)
# -> the projection to report now. The second argument is what gives banded
# fields their memory; see `bands.band_with_deadband`.
Projector = Callable[[Any, Any], Any]

_EMPTY_STEPS: Mapping[str, float] = MappingProxyType({})


@dataclass(frozen=True)
class FieldPolicy:
    """How each leaf field of one state key enters the diff.

    Every field a collector can emit belongs in exactly one of these:
    `excluded` (never compared - a timestamp, a monotonic counter, a
    free-text diagnostic), `banded` (compared at the given step with a
    full-step deadband) or `exact`. A field in none of them is a bug the
    accounting test catches before it reaches the live tick.
    """

    excluded: frozenset[str] = frozenset()
    banded: Mapping[str, float] = field(default_factory=lambda: _EMPTY_STEPS)
    exact: frozenset[str] = frozenset()

    @property
    def accounted_for(self) -> frozenset[str]:
        return self.excluded | frozenset(self.banded) | self.exact


@dataclass(frozen=True)
class Collector:
    """One registered source of tick state.

    `keys` are the top-level `collect()` keys this collector owns. They are
    declared rather than inferred so the registry can still fill them in
    with an error marker when `collect` itself blows up, instead of the
    key silently vanishing from the state (which would read as "this went
    away", not "we couldn't look").
    """

    name: str
    keys: tuple[str, ...]
    collect: Callable[[], dict[str, Any]]
    projectors: Mapping[str, Projector] = field(default_factory=lambda: MappingProxyType({}))
    policies: Mapping[str, FieldPolicy] = field(default_factory=lambda: MappingProxyType({}))


_REGISTRY: dict[str, Collector] = {}

# Hooks run once at the start of every sweep, before any collector runs.
# Used to drop per-sweep caches: two collectors legitimately want the same
# hostctl response (ZFS state feeds both the pool/dataset keys and backup
# coverage), and fetching it twice a minute forever is waste, while caching
# it without a reset would freeze the tick's view of the pool at whatever
# it saw the first time the process started.
_SWEEP_RESETS: list[Callable[[], None]] = []


def on_sweep_start(fn: Callable[[], None]) -> Callable[[], None]:
    _SWEEP_RESETS.append(fn)
    return fn


def register(collector: Collector) -> Collector:
    """Add (or replace) a collector. Registration happens by import side
    effect, the same way `agent/tools/base.py` registers tools."""
    _REGISTRY[collector.name] = collector
    return collector


def registered() -> tuple[Collector, ...]:
    return tuple(_REGISTRY.values())


def clear() -> None:  # pragma: no cover - test helper
    _REGISTRY.clear()


def safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one fetch, turning a transport failure into data.

    hostctl or a service being down must not crash the whole tick - that is
    exactly when the agent needs to keep watching. A failed fetch reports
    itself as `{"error": ...}` instead, which both keeps the tick alive and
    shows up as a real, diffable state change on the next comparison.
    """
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect_all() -> dict[str, Any]:
    """Run every registered collector and merge what they produce.

    A collector that raises (a bug in the collector itself, not a transport
    failure it already handles) has each of its declared keys filled with
    an error marker rather than being dropped - see `Collector.keys`.
    A collector may also deliberately produce nothing (an empty dict) when
    the thing it watches isn't available at all; its keys are then simply
    absent, which is what "degrade to nothing" means here.
    """
    for reset in _SWEEP_RESETS:
        reset()
    state: dict[str, Any] = {}
    for collector in registered():
        try:
            produced = collector.collect()
        except Exception as exc:
            marker = {"error": f"{type(exc).__name__}: {exc}"}
            produced = dict.fromkeys(collector.keys, marker)
        state.update(produced)
    return state


def projectors() -> dict[str, Projector]:
    out: dict[str, Projector] = {}
    for collector in registered():
        out.update(collector.projectors)
    return out


def policies() -> dict[str, FieldPolicy]:
    out: dict[str, FieldPolicy] = {}
    for collector in registered():
        out.update(collector.policies)
    return out


def project_all(
    state: dict[str, Any], previous_projection: dict[str, Any] | None
) -> dict[str, Any]:
    """Project raw state onto the fields worth diffing on.

    Only keys actually present in `state` are projected, so a partial state
    (a collector that produced nothing, or a test feeding a hand-built
    state) passes through untouched rather than gaining empty keys.
    """
    prev = previous_projection or {}
    projected = dict(state)
    for key, projector in projectors().items():
        if key in projected:
            projected[key] = projector(projected[key], prev.get(key))
    return projected


def mapping_projector(policy: FieldPolicy) -> Projector:
    """A projector for a flat `{field: value}` state key, driven entirely by
    `policy`: excluded fields are dropped, banded fields get a full-step
    deadband against their previously reported value, everything else is
    passed through for exact comparison."""

    def project(value: Any, previous: Any) -> Any:
        if not isinstance(value, dict) or "error" in value:
            return value
        prev = previous if isinstance(previous, dict) else {}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in policy.excluded:
                continue
            step = policy.banded.get(key)
            out[key] = band_with_deadband(item, prev.get(key), step) if step is not None else item
        return out

    return project


def nested_mapping_projector(policy: FieldPolicy) -> Projector:
    """`mapping_projector`, applied to each value of a `{name: {field: ...}}`
    state key (certificates by subject, datasets by name). Entries are
    matched to their previous projection by name, which is what lets the
    inner banded fields keep their memory across ticks."""
    inner = mapping_projector(policy)

    def project(value: Any, previous: Any) -> Any:
        if not isinstance(value, dict) or "error" in value:
            return value
        prev = previous if isinstance(previous, dict) else {}
        return {name: inner(item, prev.get(name)) for name, item in value.items()}

    return project


def iter_keys() -> Iterable[str]:  # pragma: no cover - trivial
    for collector in registered():
        yield from collector.keys
