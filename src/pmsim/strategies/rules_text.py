"""Heuristics over resolution rules text. Keyword based and transparent, not a model.

`risk_score` flags rules whose wording leaves room for surprise resolutions.
`mismatch_score` compares the resolution terms of two markets on different venues.
"""

from __future__ import annotations

import re
from datetime import datetime

# phrase -> weight. Each phrase counts once.
RISK_PHRASES: dict[str, float] = {
    "sole discretion": 2.0,
    "at its discretion": 2.0,
    "ambiguous": 1.5,
    "ambiguity": 1.5,
    "50-50": 2.0,
    "50/50": 2.0,
    "unknown": 1.0,
    "clarification": 1.0,
    "additional context": 1.0,
    "consensus of credible reporting": 0.25,  # boilerplate in ~60% of live rules
    "postponed": 1.0,
    "delayed": 1.0,
    "canceled": 1.0,
    "cancelled": 1.0,
    "rescheduled": 1.0,
    "may be resolved": 1.0,
    "subjective": 2.0,
    "interpretation": 1.0,
    "at the time of resolution": 0.5,
    "officially": 0.5,
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?%?")
_DOMAIN = re.compile(r"https?://(?:www\.)?([a-z0-9.-]+)", re.IGNORECASE)
_MONTHS = (
    "january february march april may june july august september october november december"
).split()


def risk_score(text: str) -> float:
    """Sum of weights of risky phrases present. 0 means none found."""
    lower = text.lower()
    return sum(w for phrase, w in RISK_PHRASES.items() if phrase in lower)


def resolution_terms(text: str) -> set[str]:
    """Numbers, months and source domains named in the rules."""
    lower = text.lower()
    terms = {n.replace(",", "") for n in _NUMBER.findall(lower)}
    terms |= {m for m in _MONTHS if m in lower}
    terms |= {d.removeprefix("www.") for d in _DOMAIN.findall(lower)}
    return terms


def mismatch_score(
    rules_a: str, rules_b: str, end_a: datetime | None, end_b: datetime | None
) -> float:
    """0 (same terms, same end) to 1 (nothing shared). Mixes term overlap and end-date gap.

    Score = 0.7 * (1 - Jaccard(terms)) + 0.3 * min(1, |end gap| / 7 days).
    A missing end date counts as the full gap.
    """
    a, b = resolution_terms(rules_a), resolution_terms(rules_b)
    union = a | b
    term_part = 1 - len(a & b) / len(union) if union else 1.0
    if end_a is None or end_b is None:
        date_part = 1.0
    else:
        date_part = min(1.0, abs((end_a - end_b).total_seconds()) / (7 * 86_400))
    return 0.7 * term_part + 0.3 * date_part
