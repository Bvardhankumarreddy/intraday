"""Post-session review (window "post"). Appends outcomes; never edits
recommendations.

For every recommendation of the day:
- EXPIRED: an appended record for CANDIDATE/WATCH recommendations whose
  validity has passed. The original record keeps its status.
- PRICE_PATH: a hypothetical outcome under the same execution rules as the
  comparison (``compare.simulate``), from completed candles only.
- PAPER_OUTCOME: the paper engine's result for the same signal, if any.

The language model then writes a ``PostSessionReview`` that separates
process quality from profitability, citing record IDs. It makes
observations only and never tunes the strategy. If the model fails, the
deterministic outcomes are still recorded.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import ValidationError

from app.agent.compare import simulate
from app.agent.llm import LLMError, cost_usd
from app.agent.prompts import REVIEW_PROMPT_VERSION, REVIEW_SYSTEM
from app.agent.schema import PostSessionReview
from app.agent.store import canonical


def post_session_review(agent, run_id: str, now: datetime) -> dict:
    st = agent.agent_store
    day = now.date().isoformat()
    recs = [r for r in st.recommendations(market_date=day) if r["window"] != "post"]
    records = []
    for r in recs:
        body = json.loads(r["body"])
        rid = r["recommendation_id"]
        if body.get("symbol"):
            vu = body.get("valid_until")
            if body["status"] in ("CANDIDATE", "WATCH") and vu and \
                    datetime.fromisoformat(vu) < now:
                st.append_outcome(rid, "EXPIRED", now, {"original_status": body["status"],
                                                        "valid_until": vu})
            path = simulate(body, agent.provider, agent.settings, agent.calendar, until=now)
            st.append_outcome(rid, "PRICE_PATH", now, path)
            if body.get("signal_id"):
                pos = agent.store.one("SELECT state, entry_price, exit_price, exit_reason, "
                                      "net_pnl FROM positions WHERE signal_id = ?",
                                      (body["signal_id"],))
                if pos is not None:
                    st.append_outcome(rid, "PAPER_OUTCOME", now, dict(pos))
        records.append({"id": rid, "symbol": body.get("symbol"), "status": body["status"],
                        "status_reasons": body["status_reasons"][:3],
                        "ai_status": body["ai_status"],
                        "outcomes": [{"id": f"O{o['id']}", "kind": o["kind"],
                                      "body": json.loads(o["body"])}
                                     for o in st.outcomes(rid)]})
    review, ai_status, model_ids = None, "NOT_REQUESTED", []
    if records:
        review, ai_status, model_ids = _model_review(agent, run_id, now, records)
    summary = {"recommendations_reviewed": len(records), "ai_status": ai_status,
               "review": review.model_dump(mode="json") if review else None,
               "model_calls": model_ids,
               "note": "observations only; the strategy is never changed from this review"}
    st.finish_run(run_id, "COMPLETED", now, None if records else "no recommendations today",
                  summary, model=agent.llm.model)
    from app.agent.report import write_run_report

    path = write_run_report(agent, run_id)
    return {"status": "COMPLETED", "ai_status": ai_status, "reviewed": len(records),
            "report": str(path)}


def _model_review(agent, run_id, now, records):
    from app.agent.workflow import NOTICE, submit_tool

    brief = {"task": f"post-session review for {now.date()}", "records": records,
             "notice": NOTICE}
    tools = [submit_tool("submit_review", PostSessionReview)]
    messages = [{"role": "user", "content": json.dumps(brief, default=str)}]
    ids = {x["id"] for x in records} | {o["id"] for x in records for o in x["outcomes"]}
    model_ids = []
    for i in range(min(2, agent.cfg.max_model_requests)):
        mid = f"R{i + 1}"
        stamp = datetime.now(agent.calendar.tz).isoformat()
        try:
            resp = agent.llm.chat(REVIEW_SYSTEM, messages, tools,
                                  {"type": "tool", "name": "submit_review"})
        except LLMError as exc:
            agent.agent_store.record_model_call({
                "run_id": run_id, "call_id": mid, "provider": agent.llm.name,
                "model": agent.llm.model, "prompt_version": REVIEW_PROMPT_VERSION,
                "started_at": stamp, "error_kind": exc.kind, "error": str(exc)[:500]})
            return None, "UNAVAILABLE", model_ids + [mid]
        agent.agent_store.record_model_call({
            "run_id": run_id, "call_id": mid, "provider": agent.llm.name, "model": resp.model,
            "prompt_version": REVIEW_PROMPT_VERSION, "started_at": stamp,
            "latency_seconds": resp.latency_seconds, "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens, "cost_usd": cost_usd(agent.settings, resp),
            "attempts": resp.attempts, "stop_reason": resp.stop_reason,
            "request_id": resp.request_id, "raw_response": canonical(resp.raw)[:100000]})
        model_ids.append(mid)
        use = next((u for u in resp.tool_uses if u.name == "submit_review"), None)
        if use is None:
            continue
        if resp.stop_reason == "max_tokens":  # cut-off output is never accepted
            messages += [{"role": "assistant", "content": resp.raw.get("content") or [
                {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id,
                                              "is_error": True,
                                              "content": "output cut off at the length "
                                                         "limit; resubmit a shorter review"}]}]
            continue
        try:
            review = PostSessionReview.model_validate(use.input)
        except ValidationError:
            messages += [{"role": "assistant", "content": resp.raw.get("content") or [
                {"type": "tool_use", "id": use.id, "name": use.name, "input": use.input}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id,
                                              "is_error": True,
                                              "content": "schema validation failed"}]}]
            continue
        bad = [c for c in review.process_observations + review.outcome_observations
               if c.basis != "interpretation" and not set(c.evidence_ids) <= ids]
        if bad:
            review.data_issues.append(f"{len(bad)} claim(s) cite unknown record IDs")
        return review, "OK", model_ids
    return None, "INVALID_OUTPUT", model_ids
