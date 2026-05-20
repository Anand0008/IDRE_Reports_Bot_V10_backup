"""Unit tests for the V10 router scoring + dispatch.

Locks the Tier 1 routing behavior applied 2026-05-19. See
local/docs/superpowers/reports/2026-05-19-router-tier1-applied.md for context.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# Make the bot importable when pytest runs from anywhere
_BOT_ROOT = Path(__file__).parent.parent.resolve()
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from agents.router import route, _is_count_intent  # noqa: E402


# ── count-intent detector ─────────────────────────────────────────────────────


def test_count_intent_basic():
    assert _is_count_intent("how many cases are overdue?")
    assert _is_count_intent("How Many disputes?")
    assert _is_count_intent("count of pending payments")
    assert _is_count_intent("total number of disputes")
    assert _is_count_intent("what is the total revenue this month")
    assert _is_count_intent("what's the number of cases in RFI")
    assert _is_count_intent("what is the average resolution time")


def test_count_intent_negatives():
    assert not _is_count_intent("show me overdue cases")
    assert not _is_count_intent("list disputes by status")
    assert not _is_count_intent("give me the dashboard")


# ── Pattern C regression (the failure mode this whole change is about) ───────


def test_pattern_c_overdue_count_routes_to_derived():
    """DD_overdue_count in Phase 1 was wrongly routed to known. Lock the fix."""
    rd = route("how many cases are currently overdue?")
    assert rd.path == "derived", (
        f"expected derived, got {rd.path} (report={rd.report}, "
        f"confidence={rd.confidence:.2f}, reasoning={rd.reasoning!r})"
    )


def test_single_word_trigger_with_count_intent_demoted():
    """Single-word triggers + 'how many' = derived (Pattern C variants)."""
    cases = [
        "how many cases are currently overdue?",  # overdue
        "how many cases are urgent right now?",  # urgent
        "how many disputes have a deadline soon?",  # deadline
    ]
    for q in cases:
        rd = route(q)
        assert rd.path == "derived", (
            f"{q!r} routed to {rd.path}/{rd.report} (score={rd.confidence:.2f})"
        )


# ── Multi-word triggers still route to known ─────────────────────────────────


def test_show_me_due_today_routes_to_known():
    """Multi-word trigger 'due today' should still hit known confidently."""
    rd = route("show me cases due today")
    assert rd.path == "known"
    assert rd.report == "due-dates"
    assert rd.confidence >= 0.85


def test_multi_trigger_match_known():
    """Two trigger phrases firing = confident known path."""
    rd = route("show me overdue cases approaching this week")
    assert rd.path == "known"
    assert rd.report == "due-dates"
    assert rd.confidence >= 0.85


def test_dashboard_overview_routes_to_known():
    """Backward-compat: dashboard-stats with multi-trigger query."""
    rd = route("show me the dashboard overview kpi summary")
    assert rd.path == "known"
    assert rd.report == "dashboard-stats"
    assert rd.confidence >= 0.85


def test_outstanding_payments_keyword():
    """Backward-compat: 'outstanding payment' is multi-word, routes known."""
    rd = route("show me outstanding payments")
    assert rd.path == "known"
    assert rd.report == "outstanding-payments"


# ── No match → derived ───────────────────────────────────────────────────────


def test_no_trigger_match_routes_to_derived():
    rd = route("give me cases grouped by status by organization")
    assert rd.path == "derived"


def test_word_boundary_prevents_false_positives():
    """'overdue' should not match inside 'overcooked', 'urgent' not inside 'urgently'.

    Word-boundary matching prevents over-eager substring hits. (The reverse
    direction, 'urgently' matching 'urgent' trigger, is a real edge case
    that current Python regex `\\b` handles naturally — both share a word
    boundary at the start.)
    """
    # 'reduce' contains 'duce' but not 'due' as a standalone word
    rd = route("reduce the overhead in our process")
    # Should not pick up due-dates' 'due date' trigger
    assert rd.path == "derived", (
        f"false-positive: {rd.path}/{rd.report} for substring 'duce'"
    )
