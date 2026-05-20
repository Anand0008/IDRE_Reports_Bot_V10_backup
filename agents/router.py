"""V10 Router — deterministic signature match + LLM fallback.

Stage 1: deterministic keyword match against config/route_signatures.json.
Stage 2: Gemini fallback when stage 1 confidence < 0.85. (TODO — Day 9.)
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SIG_PATH = Path(__file__).parent.parent / "config" / "route_signatures.json"


@dataclass
class RouterDecision:
    path: str                     # "known" | "derived" | "clarify"
    report: str | None = None
    parameters: dict = field(default_factory=dict)
    confidence: float = 0.0
    reasoning: str = ""


_SIGS_CACHE: list[dict] | None = None


def _load_signatures() -> list[dict]:
    global _SIGS_CACHE
    if _SIGS_CACHE is None:
        _SIGS_CACHE = json.loads(SIG_PATH.read_text(encoding="utf-8"))["signatures"]
    return _SIGS_CACHE


def _extract_params(query: str, sig: dict) -> dict:
    out: dict[str, Any] = {}
    q = query.lower()
    for spec in sig.get("parameter_extractors", []):
        name = spec["name"]
        # Priority-ordered choice list (first match in priority order wins,
        # regardless of position in the query string)
        if "priority_choices" in spec:
            for choice in spec["priority_choices"]:
                if re.search(rf"\b{re.escape(choice.lower())}\b", q):
                    out[name] = choice
                    break
            if name in out:
                continue
        # Regex-based extraction (leftmost match)
        if "regex" in spec:
            m = re.search(spec["regex"], q, re.IGNORECASE)
            if m:
                out[name] = m.group(1) if m.lastindex else m.group(0)
                continue
        # Phrase-extractor with int
        for phrase in spec.get("from_phrases", []):
            m = re.search(phrase["match"], q, re.IGNORECASE)
            if m:
                v = m.group(1)
                if phrase.get("extract_int"):
                    out[name] = int(v)
                else:
                    out[name] = v
                break
        # Default
        if name not in out and "default" in spec:
            out[name] = spec["default"]
    return out


_COUNT_INTENT_RE = re.compile(
    r"^\s*(how many|how much|what(?:'?s| is) the (?:total|count|number|"
    r"average)|count of|total number|number of)\b",
    re.IGNORECASE,
)


def _is_count_intent(query: str) -> bool:
    """True when the query asks for a scalar count or aggregate.

    Count-intent prompts are usually wrong to route to known-path endpoints
    because IDRE's report endpoints mostly return LISTS, not scalars.
    The derived path can generate `SELECT COUNT(*)` / `AVG(...)` directly.

    Known limitation: a few IDRE endpoints (dashboard-stats, due-dates/summary,
    case-analytics' keyMetrics block) DO return scalar bundles. We currently
    demote all count-intent + single-trigger queries to derived, which is
    safe (derived can compute counts) but slightly slower than necessary for
    those few endpoints. See `docs/superpowers/plans/2026-future-router-option-f.md`
    for the per-endpoint `result_kind` enhancement.
    """
    return bool(_COUNT_INTENT_RE.search(query))


def _score(query: str, sig: dict) -> float:
    """Compute routing confidence for one signature against the query.

    The known-path threshold is 0.85. Reaching it requires either:
      (a) two or more trigger-phrase matches (word-boundary aware), OR
      (b) one multi-word trigger phrase ("due today", "case balance").

    Single single-word triggers ("overdue", "halo", "summary") are
    deliberately under-scored (cap at 0.5 + entity bonus) to prevent
    Pattern-C style over-eager known-path routing. Count-intent prompts
    ("how many X") are demoted further when only one trigger fires.

    Failure-mode asymmetry: routing to derived when known would have
    worked just makes the query slower; routing to known when derived
    would have worked returns wrong data (Pattern C). The function biases
    toward the safe direction.
    """
    q = query.lower()
    triggers = sig.get("trigger_phrases", [])
    entities = sig.get("required_entities", [])
    if not triggers:
        return 0.0

    matched_triggers = [
        t for t in triggers
        if re.search(rf"\b{re.escape(t.lower())}\b", q)
    ]
    # Naive singular/plural for entities ("case" matches "cases")
    matched_entities = sum(
        1 for e in entities
        if re.search(rf"\b{re.escape(e.lower())}s?\b", q)
    )

    if not matched_triggers:
        return 0.0

    n = len(matched_triggers)
    if n >= 2:
        base = 0.85 + 0.05 * min(n - 2, 3) + 0.02 * matched_entities
    else:
        has_multiword = " " in matched_triggers[0]
        base = (0.85 if has_multiword else 0.5) + 0.02 * matched_entities

    # Count-intent guard: single trigger + "how many X" = derived path.
    if n == 1 and _is_count_intent(q):
        base = min(base, 0.4)

    return min(base, 1.0)


def route(query: str) -> RouterDecision:
    from tracing import get_tracer
    tracer = get_tracer()
    with tracer.start_as_current_span("v10.router.route") as span:
        sigs = _load_signatures()
        best: tuple[float, dict | None] = (0.0, None)
        for sig in sigs:
            s = _score(query, sig)
            if s > best[0]:
                best = (s, sig)
        score, sig = best
        if sig and score >= 0.85:
            result = RouterDecision(
                path="known",
                report=sig["id"],
                parameters=_extract_params(query, sig),
                confidence=score,
                reasoning=f"signature match: {sig['id']} (score={score:.2f})",
            )
        else:
            # Surface the best non-matching signature for diagnostics.
            best_name = sig["id"] if sig else "<none>"
            result = RouterDecision(
                path="derived",
                report=None,
                parameters={},
                confidence=max(score, 0.0),
                reasoning=(
                    f"best signature was {best_name} at {score:.2f} "
                    f"(need >=0.85); routing to derived path"
                ),
            )
        try:
            span.set_attribute("router.matched_signature", result.report or "<none>")
            span.set_attribute("router.path", result.path)
            span.set_attribute("router.confidence", float(result.confidence))
        except Exception:
            pass
        return result


def router_node(state: dict) -> dict:
    """LangGraph node wrapper."""
    decision = route(state.get("resolved_query") or state.get("user_query", ""))
    return {**state, "router_decision": {
        "path": decision.path,
        "report": decision.report,
        "parameters": decision.parameters,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
    }}
