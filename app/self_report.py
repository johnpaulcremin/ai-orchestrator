"""Weekly self-report: a digest the app writes about itself, delivered as a
normal owner-scoped conversation from a "📊 System report" sender.

Zero LLM calls by default — every stat below is compiled straight from the
DB (spend_log/avoided_cost_log/feedback_log/messages, plus the free-tier and
model-catalog modules' own DB-only status reads) and rendered into a
templated markdown report that costs nothing to generate. SELF_REPORT_NARRATE
(off by default, runtime-editable like any other feature flag — see
app/settings.py) adds exactly ONE cheap OPENAI_MODEL_ROUTER call on top,
writing a short narrative paragraph above the same stats — reusing
app/orchestrator_summarize.py's `_run_summary_call` plumbing (best-effort:
any failure degrades to no narrative, never breaks report generation).

Same "no background scheduler" staleness-check pattern as db_backup.py/
app/retention.py/model_catalog.py's is_due()/*_if_due() pairs — is_due()
checked on a naturally-frequent request path (GET /v1/conversations, hit on
every sidebar load), generate_if_due() only does real work on the rare call
where a week has actually passed. UNLIKE app/retention.py's maintenance,
this is per-OWNER (each caller gets their own report on their own weekly
clock from their own last-generated timestamp — see database.py's
self_report_runs table), since the report itself is owner-scoped data, not
app-wide housekeeping. Wired via FastAPI's BackgroundTasks (see
routers/conversations.py) so a due report never adds latency to the sidebar
load that happened to trigger it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import (
    correction_tracking,
    database,
    feedback,
    free_tier,
    model_catalog,
    retention,
)
from .db_backup import last_backup_at
from .fallback_reason import REASON_LABELS
from .settings import bool_setting
from .telemetry import logger

WINDOW_DAYS = 7
_INTERVAL_DAYS = 7
REPORT_TITLE_PREFIX = "📊 System report"


def is_due(owner: str | None, now: datetime | None = None) -> bool:
    """True when this owner has never had a report generated, or their last
    one is more than _INTERVAL_DAYS old. `now` is injectable for tests, same
    as app/retention.py's is_due()."""
    last = database.last_self_report_run_at(owner)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True
    return now - last_dt > timedelta(days=_INTERVAL_DAYS)


def compile_stats(owner: str | None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Every figure the report renders, compiled from the DB only — see this
    module's docstring for the zero-LLM-by-default contract."""
    usage = database.usage_summary(owner, days)
    start_month = retention.window_start_month(days)
    by_model = retention.fold_rollup_into_by_model(
        usage["by_model"], owner, start_month
    )
    by_day = retention.fold_rollup_into_by_day(usage["by_day"], owner, start_month)

    avoided_by_reason = database.avoided_cost_by_reason(owner, days)
    real_calls = sum(int(row["calls"]) for row in by_model)
    response_hits = avoided_by_reason.get("response_cache_hit", {}).get("count", 0)
    semantic_hits = avoided_by_reason.get("semantic_cache_hit", {}).get("count", 0)
    free_lane = avoided_by_reason.get(
        "free_tier", {"count": 0, "avoided_cost_usd": 0.0}
    )
    # Total requests this owner made this week, across every path a request
    # can resolve through: a real (possibly $0 free-lane) model call, or a
    # cache hit that served an answer without one. This is the denominator
    # for both cache-hit-rate figures below.
    total_requests = real_calls + response_hits + semantic_hits
    avoided_cost_total = sum(
        row["avoided_cost_usd"] for row in avoided_by_reason.values()
    )

    fb = feedback.summarize(owner, days)
    fb["by_model"] = retention.fold_rollup_into_feedback_by_model(
        fb["by_model"], owner, start_month
    )

    correction = correction_tracking.summarize(owner, days)
    correction["by_model"] = retention.fold_rollup_into_correction_by_model(
        correction["by_model"], owner, start_month
    )
    correction["overall"] = retention.fold_rollup_into_correction_overall(
        correction["overall"], owner, start_month
    )

    fallback_reasons = retention.fold_rollup_into_fallback_reasons(
        database.fallback_reason_counts(owner, days), owner, start_month
    )

    return {
        "days": days,
        "spend_usd": sum(row["cost_usd"] for row in by_day),
        "avoided_cost_usd": avoided_cost_total,
        "tokens_per_dollar": usage["tokens_per_dollar"],
        "window_tokens": usage["window_tokens"],
        "total_requests": total_requests,
        "exact_cache_hits": response_hits,
        "semantic_cache_hits": semantic_hits,
        "exact_cache_hit_rate": (
            (response_hits / total_requests) if total_requests else None
        ),
        "semantic_cache_hit_rate": (
            (semantic_hits / total_requests) if total_requests else None
        ),
        "free_lane_calls": free_lane["count"],
        "free_lane_avoided_cost_usd": free_lane["avoided_cost_usd"],
        "free_lane_status": free_tier.status(),
        "by_model": by_model,
        "feedback_by_model": fb["by_model"],
        "feedback_by_category": fb["by_category"],
        "correction_overall": correction["overall"],
        "correction_by_model": correction["by_model"],
        "correction_by_category": correction["by_category"],
        "correction_by_lane": correction["by_lane"],
        "fallback_reasons": fallback_reasons,
        "new_models": model_catalog.status()["new_models"],
        "tool_usage": database.tool_usage_counts(owner, days),
        "db_total_bytes": database.storage_stats()[1],
        "last_backup_at": last_backup_at(),
    }


def _fmt_usd(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _fmt_pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "—"


def render_markdown(stats: dict[str, Any]) -> str:
    """The zero-LLM-by-default report body: a templated markdown digest,
    costing nothing to produce no matter how often it's regenerated."""
    lines: list[str] = [f"# Weekly self-report — last {stats['days']} days", ""]

    lines += [
        "## Spend & efficiency",
        f"- Spend this week: {_fmt_usd(stats['spend_usd'])}",
        f"- Avoided cost: {_fmt_usd(stats['avoided_cost_usd'])} "
        f"({stats['exact_cache_hits']} exact-cache + {stats['semantic_cache_hits']} "
        f"semantic-cache + {stats['free_lane_calls']} free-lane hits)",
        "- Tokens per $1: "
        + (
            f"{stats['tokens_per_dollar']:,.0f}"
            if stats["tokens_per_dollar"] is not None
            else "all free"
            if stats["window_tokens"]
            else "no usage this week"
        ),
        "",
    ]

    lines += [
        "## Cache performance",
        f"- Exact cache hit rate: {_fmt_pct(stats['exact_cache_hit_rate'])}",
        f"- Semantic cache hit rate: {_fmt_pct(stats['semantic_cache_hit_rate'])}",
        f"- Out of {stats['total_requests']} total requests this week",
        "",
    ]

    lines += ["## Free-lane routing"]
    if stats["free_lane_calls"]:
        lines.append(
            f"- {stats['free_lane_calls']} answers served free this week "
            f"({_fmt_usd(stats['free_lane_avoided_cost_usd'])} saved)"
        )
    else:
        lines.append("- No free-lane answers this week.")
    if stats["free_lane_status"]:
        for row in stats["free_lane_status"]:
            lines.append(
                f"  - `{row['model']}`: {row['remaining']}/{row['quota']} remaining today"
            )
    lines.append("")

    lines += ["## Quality (👍/👎 feedback)"]
    if stats["feedback_by_model"]:
        lines.append("| Model | Rated | Down rate |")
        lines.append("|---|---|---|")
        for model, stat in sorted(stats["feedback_by_model"].items()):
            lines.append(
                f"| {model} | {stat['answers_rated']} | {_fmt_pct(stat['down_rate'])} |"
            )
    else:
        lines.append("- No rated answers this week.")
    if stats["feedback_by_category"]:
        lines.append("")
        lines.append("| Category | Rated | Down rate |")
        lines.append("|---|---|---|")
        for category, stat in sorted(stats["feedback_by_category"].items()):
            lines.append(
                f"| {category} | {stat['answers_rated']} | {_fmt_pct(stat['down_rate'])} |"
            )
    lines.append("")

    lines += [
        "## Implicit correction rate",
        "*Caveat: this is a noisy proxy, not a verified error rate — it "
        "counts a new message that reads as a correction of the prior "
        'answer (e.g. "that\'s not what I asked"), which can both miss '
        "real corrections phrased differently and occasionally misfire.*",
        "",
        f"- Overall: {_fmt_pct(stats['correction_overall']['correction_rate'])} "
        f"({stats['correction_overall']['flagged']} flagged / "
        f"{stats['correction_overall']['answers']} answers)",
    ]
    if stats["correction_by_model"]:
        lines.append("")
        lines.append("| Model | Flagged | Answers | Correction rate |")
        lines.append("|---|---|---|---|")
        for model, stat in sorted(stats["correction_by_model"].items()):
            lines.append(
                f"| {model} | {stat['flagged']} | {stat['answers']} | "
                f"{_fmt_pct(stat['correction_rate'])} |"
            )
    if stats["correction_by_category"]:
        lines.append("")
        lines.append("| Category | Flagged | Answers | Correction rate |")
        lines.append("|---|---|---|---|")
        for category, stat in sorted(stats["correction_by_category"].items()):
            lines.append(
                f"| {category} | {stat['flagged']} | {stat['answers']} | "
                f"{_fmt_pct(stat['correction_rate'])} |"
            )
    if stats["correction_by_lane"]:
        lines.append("")
        lines.append("| Lane | Flagged | Answers | Correction rate |")
        lines.append("|---|---|---|---|")
        for lane, stat in sorted(stats["correction_by_lane"].items()):
            lines.append(
                f"| {lane} | {stat['flagged']} | {stat['answers']} | "
                f"{_fmt_pct(stat['correction_rate'])} |"
            )
    lines.append("")

    lines += ["## Paid fallback causes"]
    fallback_reasons = stats["fallback_reasons"]
    if fallback_reasons:
        fallback_total = sum(row["count"] for row in fallback_reasons)
        lines.append("| Reason | Count | Share |")
        lines.append("|---|---|---|")
        for row in fallback_reasons:
            share = row["count"] / fallback_total if fallback_total else 0.0
            label = REASON_LABELS.get(row["reason"], row["reason"])
            lines.append(f"| {label} | {row['count']} | {_fmt_pct(share)} |")
    else:
        lines.append("- No fallbacks this week.")
    lines.append("")

    lines += ["## Model catalog"]
    if stats["new_models"]:
        lines.append(
            "- Newly seen since the last sync: "
            + ", ".join(f"`{m}`" for m in stats["new_models"])
        )
    else:
        lines.append("- No newly seen models since the last sync.")
    lines.append("")

    lines += ["## Tool usage this week"]
    tool_labels = {
        "web_search": "Web search",
        "code_execution": "Code execution",
        "fact_check": "Fact-check",
        "academic_search": "Academic search",
        "math_solve": "Math solve",
        "workflow": "Multi-step workflow",
    }
    for key, label in tool_labels.items():
        lines.append(f"- {label}: {stats['tool_usage'].get(key, 0)}")
    lines.append("")

    lines += ["## Housekeeping"]
    db_mb = stats["db_total_bytes"] / (1024 * 1024)
    lines.append(f"- Database size: {db_mb:,.1f} MB")
    last_backup = stats["last_backup_at"]
    lines.append(
        "- Last backup: "
        + (last_backup.strftime("%Y-%m-%d %H:%M UTC") if last_backup else "never")
    )

    return "\n".join(lines)


_NARRATE_PROMPT = (
    "Write a short (2-4 sentence) narrative summary of this app's weekly "
    "usage report for the person who runs it. Plain prose, no headers, no "
    "meta-commentary about being a summary. Highlight anything notable "
    "(cost trends, quality concerns, cache effectiveness) rather than just "
    "restating every number.\n\nReport:\n{report}"
)


def narrate(markdown: str) -> str:
    """Best-effort: one cheap router-model call writing a short narrative on
    top of the templated report. Returns '' on any failure (missing key,
    timeout, provider error) — same contract as
    orchestrator_summarize.summarize_text, whose plumbing this reuses."""
    from .orchestrator_summarize import _run_summary_call

    prompt = _NARRATE_PROMPT.format(report=markdown)
    return _run_summary_call(prompt)


def _is_meaningfully_active(stats: dict[str, Any]) -> bool:
    """Whether there's anything worth reporting this week — zero spend, zero
    cache/free-lane activity, zero feedback, and zero tool usage means an
    automatic report would just be an empty "here's your report about
    nothing" conversation, most commonly hit on a brand-new install's very
    first sidebar load before any real usage exists. Only gates the
    AUTOMATIC weekly generation (see generate_if_due) — the explicit
    "Generate now" button (generate_report, called directly) always
    generates, even for a genuinely empty week, since a deliberate click is
    its own signal that the caller wants to see it."""
    return bool(
        stats["spend_usd"]
        or stats["avoided_cost_usd"]
        or stats["total_requests"]
        or stats["feedback_by_model"]
        or any(stats["tool_usage"].values())
    )


def _persist_report(owner: str | None, stats: dict[str, Any]) -> dict[str, Any]:
    """Render, optionally narrate (SELF_REPORT_NARRATE), and persist a
    report from already-compiled stats — the shared tail end of
    generate_report and generate_if_due, factored out so generate_if_due can
    decide whether to bother (see _is_meaningfully_active) using the SAME
    stats it will go on to render, rather than compiling them twice."""
    markdown = render_markdown(stats)

    narrated = False
    if bool_setting("SELF_REPORT_NARRATE", False):
        narrative = narrate(markdown)
        if narrative:
            markdown = f"{narrative}\n\n---\n\n{markdown}"
            narrated = True

    today = datetime.now(timezone.utc).date().isoformat()
    conversation = database.create_conversation(
        f"{REPORT_TITLE_PREFIX} — {today}", owner
    )
    conversation_id = int(conversation["id"])
    database.add_message(
        conversation_id, role="assistant", content=markdown, mode_used="self_report"
    )
    database.record_self_report_run(owner)

    logger.info(
        "self_report.generated owner=%s conversation_id=%s narrated=%s",
        owner,
        conversation_id,
        narrated,
    )
    return {"conversation_id": conversation_id, "narrated": narrated}


def generate_report(owner: str | None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Compile stats, render the report, optionally narrate it
    (SELF_REPORT_NARRATE), persist it as a normal owner-scoped conversation,
    and record this run. The "Generate now" button's entry point — always
    generates, even for an empty week (see _is_meaningfully_active)."""
    stats = compile_stats(owner, days)
    return _persist_report(owner, stats)


def generate_if_due(owner: str | None) -> dict[str, Any] | None:
    """The actual entry point a request path calls (see
    routers/conversations.py's GET /v1/conversations, via BackgroundTasks so
    generating a due report never adds latency to the request that
    triggered it). A no-op (returns None, and does NOT record a run) unless
    a week has passed since this owner's last report AND there's something
    worth reporting — an owner with no activity yet keeps their existing
    is_due() state, so a report generates promptly once they have real
    activity rather than waiting out a further week."""
    if not is_due(owner):
        return None
    stats = compile_stats(owner)
    if not _is_meaningfully_active(stats):
        return None
    return _persist_report(owner, stats)
