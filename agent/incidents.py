"""Structured incident memory: what broke, what it turned out to be, what
fixed it, when - separate from `agent/brain.py`'s freeform prose notes so
"has this happened before" is a lookup, not a re-read of everything ever
written.

The point isn't storage, it's matching. Exact string matching on a symptom
almost never hits twice - "hostctl 500 on /guest/104/shell" today and
"guest_exec to 104 failing with a 500" next month describe the same fault
in different words. Matching here is keyed on two things instead: the
affected `component` (an exact, case-insensitive match - the model chooses
the same short names it always has, e.g. "hostctl", "jellyfin", "mail") and
a normalised, order-independent token overlap over the `symptom` text. That
catches a paraphrase; it does not require one.
"""

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from agent.store import Store

# Small, deliberately short stopword list: symptom text is short (a
# sentence, not a paragraph), so even a few common words dominating the
# token set would swamp the words that actually distinguish one fault from
# another.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "by", "for", "with", "without", "and",
    "or", "but", "not", "no", "it", "its", "this", "that", "these", "those",
    "from", "as", "than", "then", "when", "while", "into", "onto", "out",
    "up", "down", "over", "under", "again", "so", "if", "does", "did", "do",
}

_WORD = re.compile(r"[a-z0-9]+")

# Below this Jaccard score a match is not offered - a handful of shared
# common words between two unrelated symptoms would otherwise show up as a
# false "similar incident found".
DEFAULT_MIN_SCORE = 0.2
# A same-component match is worth offering even on a weaker token overlap:
# two symptoms about the same component are more likely to be the same
# recurring fault than two symptoms that merely share words.
_COMPONENT_MATCH_BONUS = 0.25


def normalize_symptom(text: str) -> frozenset[str]:
    """Lowercase, tokenize, drop stopwords and single characters. Returns a
    set (order and repetition thrown away on purpose) so "hostctl 500 on
    guest 104" and "guest 104: hostctl returned a 500" score identically.
    """
    words = _WORD.findall(text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class IncidentMatch:
    score: float
    incident: dict[str, Any]


def record(store: Store, *, component: str, symptom: str, cause: str, fix: str) -> dict[str, Any]:
    """Persist one resolved incident. All four fields are the model's own
    words - this module never generates or edits them, it only stores and
    later scores them."""
    component = component.strip()
    symptom = symptom.strip()
    cause = cause.strip()
    fix = fix.strip()
    if not component:
        raise ValueError("component must not be blank")
    if not symptom:
        raise ValueError("symptom must not be blank")
    if not cause:
        raise ValueError("cause must not be blank")
    if not fix:
        raise ValueError("fix must not be blank")
    incident_id = store.record_incident(component=component, symptom=symptom, cause=cause, fix=fix)
    return {
        "id": incident_id,
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "component": component,
        "symptom": symptom,
        "cause": cause,
        "fix": fix,
    }


def find_similar(
    store: Store,
    *,
    component: str,
    symptom: str,
    limit: int = 5,
    min_score: float = DEFAULT_MIN_SCORE,
    candidate_limit: int = 500,
) -> list[IncidentMatch]:
    """Score every recent incident against `component`/`symptom` and return
    the best `limit` matches at or above `min_score`, highest score first.

    Scoring: token-overlap (Jaccard) on the normalised symptom text, plus a
    flat bonus when `component` matches exactly (case-insensitively) - a
    weaker word overlap is still worth surfacing when it happened to the
    same component, the same way a human would weight "this happened to
    hostctl before" even from a loosely-worded old report.
    """
    target_tokens = normalize_symptom(symptom)
    target_component = component.strip().lower()
    scored: list[IncidentMatch] = []
    for row in store.list_incidents(limit=candidate_limit):
        row_tokens = normalize_symptom(row["symptom"])
        score = _jaccard(target_tokens, row_tokens)
        if row["component"].strip().lower() == target_component:
            score += _COMPONENT_MATCH_BONUS
        if score >= min_score:
            scored.append(IncidentMatch(score=min(score, 1.0), incident=row))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:limit]
