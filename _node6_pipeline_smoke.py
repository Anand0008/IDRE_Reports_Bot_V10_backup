"""Node 6 pipeline smoke test — single-prompt end-to-end run.

NOT part of V10 production. Calls the harness entrypoint directly to
exercise the full derived-path pipeline (router → context_loader →
ambiguity → clarification → schema_mapper + platform_context →
schema_verifier → sql_writer (+ Node 6 tools changes) → sql_validator
→ executor → response/output formatter).

Hits real Gemini API + real RDS staging via .env. Read-only — the
pipeline is SELECT-only by design.

Run:
    py -3.11 _node6_pipeline_smoke.py
    py -3.11 _node6_pipeline_smoke.py "your custom prompt here"
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

# Force UTF-8 stdout so we can print ≤ ≥ → … bullets from agent traces
# without Windows cp1252 blowing up.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Silence the OTEL exporter's "connection refused" warnings — no
# collector is running locally. Spans are still emitted; we just
# drop the export attempts.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

HERE = Path(__file__).parent.resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


DEFAULT_PROMPT = "show me 5 most recent cases"


def _fmt_block(title: str, body: str, max_lines: int = 40) -> str:
    """Print a labelled section with a max-line trim for very long fields."""
    lines = body.splitlines() if body else ["(empty)"]
    shown = lines[:max_lines]
    truncated = len(lines) > max_lines
    out = [f"\n{'=' * 78}", f"  {title}", "=" * 78]
    out.extend(shown)
    if truncated:
        out.append(f"... ({len(lines) - max_lines} more lines)")
    return "\n".join(out)


def _summarize_agent_trace(trace: list[dict]) -> str:
    """One-line-per-agent summary of the trace."""
    if not trace:
        return "(no trace entries)"
    rows = []
    for i, e in enumerate(trace, 1):
        agent = e.get("agent", "?")
        status = e.get("status", "?")
        summary = (e.get("summary") or "").splitlines()[0][:120]
        rows.append(f"{i:2}. [{status:>4}] {agent:<22} {summary}")
        detail = e.get("detail") or []
        for d in detail[:3]:
            d_str = str(d)
            if len(d_str) > 110:
                d_str = d_str[:107] + "..."
            rows.append(f"        · {d_str}")
        if len(detail) > 3:
            rows.append(f"        · (+{len(detail) - 3} more detail lines)")
    return "\n".join(rows)


def _summarize_data(data) -> str:
    if data is None:
        return "(None)"
    if isinstance(data, dict):
        keys = list(data.keys())[:10]
        return f"dict with {len(data)} key(s); first: {keys}"
    if isinstance(data, list):
        if not data:
            return "list[0] — empty"
        head = data[:3]
        return f"list[{len(data)}], first {len(head)} row(s):\n" + json.dumps(head, indent=2, default=str)
    return f"{type(data).__name__}: {str(data)[:200]}"


def run_one(prompt: str, user_role: str = "MA") -> dict:
    print(f"\n{'#' * 78}")
    print(f"# PROMPT: {prompt!r}")
    print(f"# user_role: {user_role}")
    print("#" * 78)

    from harness_entrypoint import run_query_v10

    t0 = time.monotonic()
    try:
        result = run_query_v10(prompt, user_role=user_role)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"\n[!!!] Pipeline raised after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return {"_exception": str(exc)}

    elapsed = time.monotonic() - t0
    print(f"\n[done] pipeline returned in {elapsed:.1f}s")

    router = result.get("router_decision") or result.get("_v10_router_decision") or {}
    sql = result.get("sql") or ""
    row_count = result.get("row_count", 0)
    trace = result.get("agent_trace") or []
    formatted = result.get("formatted_response") or ""
    err = result.get("error_message") or ""
    fallback = result.get("fallback_reason")
    data = result.get("data")

    print(_fmt_block("ROUTER DECISION", json.dumps(router, indent=2, default=str)))
    if fallback:
        print(_fmt_block("FALLBACK REASON (known→derived)", fallback))
    print(_fmt_block("GENERATED / VALIDATED SQL", sql or "(none)"))
    print(_fmt_block("ROW COUNT", str(row_count)))
    print(_fmt_block("DATA (head)", _summarize_data(data)))
    print(_fmt_block("AGENT TRACE", _summarize_agent_trace(trace)))
    print(_fmt_block("FORMATTED RESPONSE", formatted or "(none)", max_lines=30))
    if err:
        print(_fmt_block("ERROR MESSAGE", err))

    return result


BATCH_PROMPTS = [
    "how many cases were created in the last 7 days",
    "list arbitrators with most active cases",
    "top 5 organizations by case count",
    "cases assigned to me with status pending",
]


# 12 known-path prompts — one per report in config/route_signatures.json.
# Picks a clear trigger phrase from each report so the router classifies as
# known (path=known, confidence>0). The actual IDRE API call may fail if the
# staging API isn't reachable from this network — in that case
# harness_entrypoint falls through to derived. The script reports whichever
# happens so we can see router behaviour AND fallback behaviour.
KNOWN_PATH_PROMPTS = [
    ("due-dates",                   "show me overdue disputes by urgency"),
    ("outstanding-payments",        "list outstanding payments for top 20 cases"),
    ("case-balance",                "what is the case balance for active disputes"),
    ("unpaid-disputes",             "show me unpaid disputes"),
    ("idre-payouts",                "list idre payouts for last month"),
    ("dashboard-stats",             "give me the dashboard overview"),
    ("cms-payments",                "list cms payment activity"),
    ("case-analytics",              "show case analytics for closed disputes"),
    ("team-performance",            "show team performance for this quarter"),
    ("recent-activity",             "what's the recent activity in the system"),
    ("auditing/daily-funds",        "show me daily funds for auditing"),
    ("auditing/daily-transactions", "list daily transactions for the audit log"),
]


# 15 derived-path prompts — the original 5 + 10 new ones covering a wider
# shape spectrum (aggregates, JOINs, window queries, identity filters,
# cross-tab patterns). Picked to exercise the agents that Node 6.1 just
# touched (sql_writer parser + tool loop) under varied retry conditions.
DERIVED_PATH_PROMPTS = [
    "show me 5 most recent cases",
    "how many cases were created in the last 7 days",
    "list arbitrators with most active cases",
    "top 5 organizations by case count",
    "cases assigned to me with status pending",
    "what is the average resolution time per dispute type",
    "show me cases where payment is overdue by more than 30 days",
    "list the top 10 health plans by dispute volume this quarter",
    "count of closed cases this month grouped by closure reason",
    "find disputes with amount greater than $5000 that are still open",
    "show payment history for case DISP-JB4XBW8",
    "list providers with most disputed claims in the last 60 days",
    "what's the total settlement amount paid out last month",
    "show me cases waiting for arbitrator decision longer than 14 days",
    "compare ip and nip win rates by region",
]


def _verdict(result: dict) -> str:
    """One-line verdict per result for the summary table."""
    exc = (result.get("_exception") or "")[:80]
    if exc:
        return f"EXCEPTION: {exc}"
    err = (result.get("error_message") or "")[:80]
    if err:
        return f"ERROR: {err}"
    router = result.get("router_decision") or result.get("_v10_router_decision") or {}
    path = router.get("path", "?")
    rows = result.get("row_count", 0)
    fallback = result.get("fallback_reason")
    if path == "known":
        # Known-path successful API call
        idre_status = result.get("_v10_idre_status") or result.get("idre_status", "?")
        return f"KNOWN ok · IDRE status {idre_status}"
    if fallback:
        return f"KNOWN→DERIVED fallback · {rows} row(s) · fallback: {fallback[:50]}"
    sql_len = len(result.get("sql") or "")
    return f"DERIVED ok · {rows} row(s) · SQL {sql_len} chars"


def run_batch(prompts, label: str = "BATCH", user_role: str = "MA"):
    """Generic batch runner. prompts is either list[str] or list[tuple[str,str]]
    where the tuple is (expected_route, prompt)."""
    results = []
    for i, item in enumerate(prompts, 1):
        if isinstance(item, tuple):
            expected, p = item
        else:
            expected, p = None, item
        header = f"@@ {label} {i}/{len(prompts)}"
        if expected:
            header += f" (expected: {expected})"
        print(f"\n\n{'@' * 78}\n{header}\n{'@' * 78}")
        result = run_one(p, user_role=user_role)
        result["_expected_route"] = expected
        result["_prompt"] = p
        results.append(result)

    print(f"\n\n{'=' * 78}\n  {label} SUMMARY ({len(prompts)} prompt(s))\n{'=' * 78}")
    for i, r in enumerate(results, 1):
        verdict = _verdict(r)
        expected = r.get("_expected_route") or ""
        exp_tag = f"[{expected:30s}] " if expected else ""
        print(f"  {i:2d}. {exp_tag}{verdict[:90]}")
        print(f"      prompt: {r['_prompt']}")
    return results


def run_full():
    """The full e2e smoke pass: 12 known + 15 derived = 27 prompts."""
    print("=" * 78)
    print("  V10 FULL SMOKE — 12 known-path + 15 derived-path = 27 prompts")
    print("=" * 78)

    known_results = run_batch(KNOWN_PATH_PROMPTS, label="KNOWN")
    derived_results = run_batch(DERIVED_PATH_PROMPTS, label="DERIVED")

    print(f"\n\n{'#' * 78}\n#  END-TO-END SUMMARY\n{'#' * 78}\n")

    known_routed_correctly = sum(
        1 for r in known_results
        if (r.get("router_decision") or r.get("_v10_router_decision") or {}).get("path") == "known"
    )
    known_fell_through = sum(1 for r in known_results if r.get("fallback_reason"))
    known_exceptions = sum(1 for r in known_results if r.get("_exception"))
    print(f"KNOWN ({len(known_results)} prompts):")
    print(f"  Router classified as known:   {known_routed_correctly}/{len(known_results)}")
    print(f"  Fell through to derived:      {known_fell_through}")
    print(f"  Unhandled exceptions:         {known_exceptions}")

    derived_ok = sum(
        1 for r in derived_results
        if not r.get("_exception") and not r.get("error_message") and r.get("row_count", 0) >= 0
        and not r.get("fallback_reason")
    )
    derived_err = sum(1 for r in derived_results if r.get("error_message") and not r.get("_exception"))
    derived_exc = sum(1 for r in derived_results if r.get("_exception"))
    print(f"DERIVED ({len(derived_results)} prompts):")
    print(f"  Completed (any row count):    {derived_ok}/{len(derived_results)}")
    print(f"  Validator/executor rejected:  {derived_err}")
    print(f"  Unhandled exceptions:         {derived_exc}")

    return known_results, derived_results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--full":
        run_full()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--known":
        run_batch(KNOWN_PATH_PROMPTS, label="KNOWN")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--derived":
        run_batch(DERIVED_PATH_PROMPTS, label="DERIVED")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        results = run_batch(BATCH_PROMPTS, label="BATCH")
        sys.exit(0 if not any(r.get("_exception") for r in results) else 2)
    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    user_role = sys.argv[2] if len(sys.argv) > 2 else "MA"
    result = run_one(prompt, user_role=user_role)
    sys.exit(0 if not result.get("_exception") else 2)
