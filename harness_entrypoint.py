"""V10 single-call entrypoint, harness-friendly.

Flow:
  1. Router decides path
  2. If known: idre_api_client -> normalizer -> return {data: body}
  3. If derived: V10 SQL flow (delegates to core.orchestrator.run_query for Day 5;
     Days 7-8 will replace with the rewritten sql_writer + tools)
  4. If clarify: return immediate clarification error

Backward compat: still exports `run(prompt)` for V8-style harness usage.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent.resolve()
os.chdir(str(HERE))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from agents.router import route
from agents.idre_api_client import IdreApiClient
from agents.response_normalizer import normalize


def run_query_v10(
    prompt: str,
    now_anchor=None,
    user_role: str = "MA",
    feedback_correction_context: dict | None = None,
    is_feedback_retry: bool = False,
) -> dict:
    """Single-call run for harness usage.

    Args:
        prompt: user NL query
        now_anchor: optional temporal anchor for `:now` substitution.
            Accepts a datetime, an object with .isoformat()/.iso/.now_iso,
            or a plain ISO string. Defaults to datetime.now(UTC).
        user_role: V10 role code (MA, PA, PS, AC, AM, CB, VT, VO, DQD).
        feedback_correction_context: when set together with
            is_feedback_retry=True, exercises Flow B (feedback retry).
            Mirrors the dict app.py builds in _submit_and_retry.
        is_feedback_retry: see above.
    """
    from tracing import get_tracer, redact
    tracer = get_tracer()
    with tracer.start_as_current_span("v10.query") as span:
        try:
            span.set_attribute("query.prompt", redact(prompt)[:500] if isinstance(prompt, str) else "")
            span.set_attribute("query.user_role", user_role)
        except Exception:
            pass
        rd = route(prompt)
        try:
            span.set_attribute("query.path", rd.path)
            span.set_attribute("router.report", rd.report or "")
            span.set_attribute("router.confidence", float(rd.confidence))
        except Exception:
            pass
        if rd.path == "known":
            with tracer.start_as_current_span("v10.known.api_call") as known_span:
                try:
                    client = IdreApiClient()
                    resp = client.call(rd.report, rd.parameters)
                except Exception as e:
                    try:
                        known_span.set_attribute("known.error", str(e)[:200])
                    except Exception:
                        pass
                    return _run_derived(
                        prompt, rd, user_role,
                        error=str(e),
                        now_anchor=now_anchor,
                        feedback_correction_context=feedback_correction_context,
                        is_feedback_retry=is_feedback_retry,
                    )
                body = resp.get("body") if isinstance(resp.get("body"), dict) else {}
                try:
                    known_span.set_attribute("known.idre_status", resp.get("status_code", 0))
                except Exception:
                    pass
                return {
                    **body,
                    "_v10_router_decision": {
                        "path": rd.path, "report": rd.report,
                        "parameters": rd.parameters, "confidence": rd.confidence,
                    },
                    "_v10_normalized": normalize(body),
                    "_v10_idre_status": resp["status_code"],
                }
        else:
            # Note: router.route() only returns "known" or "derived" today.
            # The "clarify" path is handled inside the LangGraph by
            # clarification_agent_node after derived routing, not at this
            # layer. If router-level clarification is ever wired (spec §4
            # stage-2 LLM fallback), restore an elif clarify branch here.
            return _run_derived(
                prompt, rd, user_role,
                now_anchor=now_anchor,
                feedback_correction_context=feedback_correction_context,
                is_feedback_retry=is_feedback_retry,
            )


def _run_derived(
    prompt: str,
    rd,
    user_role: str,
    error: str | None = None,
    now_anchor=None,
    feedback_correction_context: dict | None = None,
    is_feedback_retry: bool = False,
) -> dict:
    """V10 derived path — delegates to core.orchestrator.run_query.

    Threads now_anchor + feedback_correction_context into the orchestrator
    so the harness can exercise Flow B (feedback retry) and so the
    pipeline knows what `:now` means for `find_filter_pattern` + executor
    SQL substitution.
    """
    from tracing import get_tracer
    tracer = get_tracer()
    with tracer.start_as_current_span("v10.derived.orchestrator") as span:
        from core.orchestrator import run_query
        state = run_query(
            user_query=prompt,
            session_id="harness",
            user_role=user_role,
            now_anchor=now_anchor,
            feedback_correction_context=feedback_correction_context,
            is_feedback_retry=is_feedback_retry,
        )
        try:
            span.set_attribute("derived.row_count", state.get("row_count", 0))
            span.set_attribute("derived.has_sql", bool(state.get("validated_sql") or state.get("generated_sql")))
        except Exception:
            pass
        return {
            "router_decision": {
                "path": "derived", "report": rd.report,
                "parameters": rd.parameters, "confidence": rd.confidence,
            },
            "data": state.get("query_result") or [],
            "sql": state.get("validated_sql") or state.get("generated_sql") or "",
            "row_count": state.get("row_count", 0),
            "agent_trace": state.get("agent_trace", []),
            "formatted_response": state.get("formatted_response", ""),
            "error_message": state.get("error_message", ""),
            "fallback_reason": error,
        }


def run(prompt: str, user_role: str = "MA") -> dict:
    """Backward-compat alias for V8-style callers."""
    return run_query_v10(prompt, user_role=user_role)


if __name__ == "__main__":
    import json
    r = run_query_v10(sys.argv[1] if len(sys.argv) > 1 else "show me the dashboard overview")
    print(json.dumps({
        "router": r.get("router_decision"),
        "idre_status": r.get("idre_status"),
        "row_count": r.get("row_count"),
        "data_kind": (
            f"dict(keys={list(r['data'].keys())[:6]})" if isinstance(r.get("data"), dict)
            else f"list[{len(r['data'])}]" if isinstance(r.get("data"), list)
            else type(r.get("data")).__name__
        ),
    }, indent=2, default=str))
