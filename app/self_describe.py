"""Self-description: grounds a "what can you do / do you support X / what
models do you use" question in this app's REAL configured state, instead of
letting the model guess (or flatly confabulate — it has no training data on
a private, self-hosted app named "ai-orchestrator").

Cross-provider tool, same pattern as math_solve.py: `app_capabilities` is
offered to the model as a function/custom tool (OpenAI Responses API
`function`, Anthropic Messages API custom tool-use) whenever SELF_DESCRIBE
is on and the model's provider supports one (see orchestrator._SELF_DESCRIBE_TOOL_PROVIDERS)
— the MODEL decides when a question is actually about the app itself,
instead of a phrase list guessing on the app's behalf. A call is executed
immediately, server-side (capabilities_snapshot() reads local config only —
no external call, no LLM tokens spent computing it), the moment it's
extracted from the model's response — same "no confirmation needed, no
persisted pending state" reasoning as math_solve (see that module's
docstring): capabilities_snapshot() has no real-world side effects either.

Still NOT a real cross-provider function-calling round trip in the fullest
sense: this codebase's OpenAI/Anthropic dispatch chain (see
orchestrator_calls.py/providers.py) never sends a tool call's *result* back
to the model for a second turn — math_solve and this tool both compute
their result AFTER the model's one real answer and fold it in as an
appended note (see orchestrator._compose_answer_with_notes call sites),
rather than the model itself narrating the result in its own words. The
note carries the real ground truth regardless of what the model's own text
says, which is what actually prevents a wrong answer from being the last
word on the subject — see test_run_orchestrator_appends_real_data_even_when_model_confabulates.

A provider with no hosted/custom tool-calling wired up here at all (every
LiteLLM-routed model — Gemini, Bedrock, Mistral, Groq, Ollama, local
endpoints) falls back to the same phrase-heuristic trigger this module used
exclusively before (see looks_like_capabilities_request) — same
"heuristic fallback for a provider with no native tool" reasoning
orchestrator_tools._looks_like_image_request already uses for Gemini image
generation.

The cacheable system prefix (see context_builder.py) gets a short, STATIC
identity + tool-hint line (_CAPABILITIES_IDENTITY_LINE below) whenever
SELF_DESCRIBE is on — never live numbers (a remaining-budget figure baked
into a prompt-cache-eligible prefix would either go stale across turns or
bust the cache every time it changed). The identity line just tells the
model the tool MIGHT exist and when to reach for it; the actual verified
numbers only ever appear in the appended note, computed fresh per turn.
Being static, it cannot know whether the model that ends up answering was
actually offered the tool — so it says "if it is among the tools available
to you" rather than issuing an order a tool-less model cannot obey. See the
comment on CAPABILITIES_IDENTITY_LINE for the failure that wording prevents.

"RAG-seed app docs" is out of scope here: app/rag_library.py has no
ownerless/system-scoped document concept today (every document requires an
owner and a budget-charged embedding call) — seeding is instead a per-owner
action (see the Library modal's "Seed library with app docs" button /
POST /v1/library/seed-app-docs), not a system-wide document.
"""

from __future__ import annotations

from typing import Any

from . import codebase_inventory

# codebase_inventory is the ONE app-internal import that is safe at module
# level here: it imports nothing from this package (stdlib ast/re/pathlib
# only — see its docstring's "parsed, never imported" note), so it cannot
# participate in the cycle described below.
#
# NOTE: every OTHER app-internal import below (settings, free_tier, budget,
# database, schemas) is LAZY — inside the function that needs it, never at
# module level. providers.py imports THIS module's
# APP_CAPABILITIES_TOOL_DESCRIPTION/app_capabilities_input_schema for the
# Anthropic custom-tool definition (same as it already does for
# math_solve.py's), and settings.py imports providers.py — so ANY of these
# at module level here would be a circular import the moment it's reached
# transitively (settings.py directly; free_tier.py and schemas.py both
# import settings.py themselves). Same reasoning as orchestrator_tools.py
# keeping _math_solve_enabled() out of math_solve.py itself (see that
# function's docstring) and math_solve.py itself having zero app-internal
# imports at module level.

# Bumped alongside each CHANGELOG release cut (see CHANGELOG.md) — the one
# source of truth self_describe/GET /v1/capabilities report, so "what
# version are you" never depends on the answering model's own guess.
APP_VERSION = "0.3.0"

# Prepended to the cacheable system prefix (see context_builder.py) whenever
# SELF_DESCRIBE is on — deliberately static (no live numbers: see module
# docstring) so it doesn't bust prompt caching, and short enough that
# turning the flag on costs a handful of tokens, not a paragraph.
# CONDITIONAL by design ("if it is among the tools available to you"), and
# that clause is load-bearing.
#
# This line is STATIC — it goes into the cacheable prefix, which is assembled
# in routers/messages/ask.py BEFORE routing has picked a model, so it cannot
# know which provider will answer. But the tool it names is gated per provider
# (_SELF_DESCRIBE_TOOL_PROVIDERS = openai/anthropic), and a LiteLLM-routed
# model — Gemini, Ollama, Bedrock — is never offered it. Two more ways to end
# up in that mismatch: a heuristic-path turn where the phrase trigger did not
# fire, and a FAILOVER, which retries a different model with the tool flags
# derived from the PRIMARY (see orchestrator_calls._fallback_models).
#
# Observed live: an Ollama budget-tier turn failed over to Claude, which got
# this line but no tool, and emitted a made-up text invocation of it into the
# answer body — a bare token where the answer should have been. Making the
# line provider-aware would fix that, and cost more than it saves: the prefix
# would change every time auto-routing sent consecutive turns to different
# tiers, busting the prompt cache this whole split exists to keep warm.
#
# So the line stays static and stops giving an order that cannot always be
# followed. No "$" anywhere in it, deliberately — see
# test_identity_line_present_when_enabled, which pins that no live figure is
# ever baked into the cacheable prefix.
CAPABILITIES_IDENTITY_LINE = (
    "You are AI Orchestrator, a cost-aware multi-model router. For "
    "questions about your own features, configuration or limits, call the "
    "app_capabilities tool if it is among the tools available to you. If it "
    "is not, answer from what this prompt already tells you and say plainly "
    "what you cannot confirm — never write a tool call out as text."
)

# A compact, static architecture summary — folded into capabilities_snapshot()
# and format_note() so a model asked to "suggest improvements" (or anything
# else that benefits from knowing HOW this app is built, not just what it's
# configured to do) doesn't re-propose organs the patient already has, e.g.
# suggesting LiteLLM/a RAG pipeline/SQLite as new additions to an app that
# already runs on all three. Deliberately static (no version numbers, no
# live counts) so it's as cheap to include as any other fixed string here —
# see the module docstring's "no live numbers in the cacheable prefix" rule,
# which this doesn't touch since it's only ever appended in format_note()'s
# note, never folded into CAPABILITIES_IDENTITY_LINE.
INTERNALS_SUMMARY = (
    "Built on: OpenAI and Anthropic models are called natively; every other "
    "provider (Gemini, Bedrock, Mistral, Groq, local Ollama models, and any "
    "generic OpenAI-compatible endpoint) is routed through LiteLLM. All data "
    "— conversations, messages, settings, spend, feedback — lives in a "
    "single local SQLite database, with spend and feedback recorded as "
    "append-only ledgers. Retrieval-augmented answers draw on a per-owner "
    "document library matched by brute-force cosine similarity over stored "
    "embeddings — deliberately no vector database. Responses can be exact-"
    "cached (identical request) or semantically cached (near-duplicate "
    "question); a free-tier lane routes eligible questions to free-quota "
    "models before spending budget; and an opt-in workflow mode can break a "
    "multi-step request into sequential sub-steps with its own synthesis "
    "pass, instead of one undifferentiated answer."
)

# What the INTERFACE can do, as opposed to INTERNALS_SUMMARY's "how it's
# built" and _flags()' bare flag names. Without this, a model asked "what can
# this app do?" or "what's missing?" had nothing to go on for the UI half of
# the answer and invented plausible-sounding features — and, worse, proposed
# "improvements" that already ship. Every clause below was read off the
# frontend, not assumed: see frontend/src/MessageList.tsx (markdown, inline
# images, the tool cards, the memory indicator, the per-message badges, the
# edit/branch controls), Sidebar.tsx (search), Library.tsx, Share.tsx, and
# frontend/public/manifest.webmanifest.
#
# Deliberately one dense paragraph, not a bulleted feature brochure: this
# rides on every SELF_DESCRIBE call, so it is written to be cheap.
_UI_ALWAYS = (
    "answers render as markdown (GitHub-flavoured — tables, lists, task "
    "lists — with a copy button on every code block); each answer carries "
    "badges for the routing mode that served it, its token count and its "
    "cost, or a cached/free badge when it was not billed; any message can be "
    "copied, linked to, bookmarked, rated, read aloud, and edited or branched "
    "into a new conversation from that point; the newest answer can be "
    "regenerated, optionally against a different model; a whole conversation "
    "can be duplicated, searched within, exported as Markdown, and published "
    "as a read-only share link that can be revoked at any time and can also "
    "expire on its own after a configurable number of days (see the data "
    "policy below for whether an expiry is currently set); conversations are "
    "searchable across "
    "the sidebar; and the frontend ships a web app manifest, so it installs "
    "to a phone home screen as a standalone app"
)

# Each clause is claimed ONLY when its flag is actually on — the inverse of
# _disabled_features(), and the reason this is assembled per call rather than
# being another static string like INTERNALS_SUMMARY. Claiming a switched-off
# capability is exactly the confabulation this module exists to stop.
_UI_FLAGGED: tuple[tuple[str, str], ...] = (
    ("IMAGE_GENERATION", "generated images display inline in the answer"),
    (
        "CODE_EXECUTION",
        "a collapsible card shows the code that was run and its output, and a "
        "generated .xlsx/.csv previews inline as a scrollable table — sheet "
        "name, true row/column counts, and an explicit note when only the "
        "first rows are shown — beside its download link",
    ),
    (
        "WEB_SEARCH",
        "a collapsible card shows the search queries that were issued and the "
        "sources they returned",
    ),
    (
        "FACT_CHECK",
        "a collapsible card shows each checked claim with its rating and publisher",
    ),
    (
        "MATH_SOLVE",
        "a collapsible card shows each computed expression, its exact result "
        "and which engine produced it",
    ),
    ("ACADEMIC_SEARCH", "a collapsible card lists the scholarly works found"),
    (
        "CROSS_CONVERSATION_MEMORY",
        "an indicator names which past conversations an answer drew on",
    ),
    (
        "RAG_LIBRARY",
        "a document library panel manages the documents retrieval draws on, "
        "and an answer names the ones it used",
    ),
)


def _ui_capabilities() -> str:
    """The interface's real capabilities, with every optional one gated on
    its actual flag — so this never claims something the current
    configuration has switched off (the same contract _disabled_features()
    upholds from the other direction).

    The derived panel list (app/codebase_inventory.ui_panels) is appended
    UNGATED, unlike the module inventory: it costs ~105 tokens, and it is
    not adding a new claim so much as correcting one already being sent.
    _UI_ALWAYS is hand-written and never mentioned the Usage panel, so a
    model asked what this app lacked reported it had no usage analytics and
    proposed building the charts that panel already draws. A paragraph
    someone has to remember to update is exactly the thing this replaces.
    """
    flags = _flags()
    extras = [clause for key, clause in _UI_FLAGGED if flags.get(key)]
    text = f"Interface: {_UI_ALWAYS}"
    if extras:
        text += f". With the features currently enabled: {'; '.join(extras)}"
    text += "."
    panels = codebase_inventory.format_ui_lines()
    if panels:
        text += f" {panels}"
    return text


APP_CAPABILITIES_TOOL_DESCRIPTION = (
    "Get this app's REAL, live configuration and capabilities — how it's "
    "built internally, the actual enabled features, which optional "
    "features are available but currently disabled (and what they'd do), "
    "the inventory of subsystems ALREADY IMPLEMENTED in its codebase, "
    "effective model map, known request limits, your own remaining daily "
    "budget, and free-lane quota status — instead of guessing or inventing "
    "details about a private, self-hosted app you have no training data "
    "on. Call this ONLY for a direct question about the app itself: what "
    "you (this app) can do, what models you use, whether you support some "
    "feature, what your limits are, what version you are, how much budget "
    "is left, when a disabled feature would have helped answer the "
    "user's question (so you can flag it as available-but-off rather than "
    "silently doing without or proposing to add something that already "
    "exists), or — ESPECIALLY — when asked to critique this app, list its "
    "weaknesses, or suggest improvements to it: answering that from priors "
    "about self-hosted chat apps in general reliably proposes work that is "
    "already done, and this tool is the only way to know what exists. Do "
    "NOT call this for a question about a SPECIFIC PREVIOUS "
    "answer or turn in this conversation — e.g. 'which model answered "
    "that', 'why did that take two attempts', 'why did it fail', 'what "
    "took so long' — this tool has no memory of past turns, only the "
    "app's current configuration; answer those directly from the "
    "conversation itself. Also do not call this for a general question "
    "about AI/LLMs that isn't about this specific app (e.g. 'what's the "
    "best coding model right now', 'how do transformers work'). Takes no "
    "arguments."
)


def app_capabilities_input_schema() -> dict[str, Any]:
    """No meaningful arguments — the tool's whole point is a fixed snapshot
    of server-side state, not anything the model would parameterize.
    An empty `properties` object (rather than omitting `parameters`
    entirely) is what both the OpenAI Responses API and Anthropic Messages
    API expect for a zero-argument tool."""
    return {"type": "object", "properties": {}, "additionalProperties": False}


def self_describe_enabled() -> bool:
    """Opt-in: SELF_DESCRIBE=true (env, or a saved Settings override — same
    override > env > default chain as any other toggle). Off by default,
    same as every other feature here that folds extra context/notes into an
    answer (CROSS_CONVERSATION_MEMORY, RAG_LIBRARY)."""
    from .settings import bool_setting

    return bool_setting("SELF_DESCRIBE", False)


# A deliberately narrow, high-precision phrase list — same design as
# fact_check._FACT_CHECK_PHRASES/academic_search._ACADEMIC_SEARCH_PHRASES:
# errs toward missing a request over over-triggering an extra note on an
# ordinary question.
#
# Audited for the same class of bug the fact_check phrase-list post-mortem
# found (a bare/generic fragment that fires on an unrelated sentence just
# because the words happen to co-occur — see CHANGELOG.md's "is this claim"
# entry). Three phrases were removed for exactly that reason, each replaced
# by nothing (the remaining, more-qualified phrases already cover the
# legitimate question they were meant to catch):
#   - "what are you"   -- matched ANY "what are you doing/thinking/working
#     on/talking about" follow-up; "tell me about yourself" already covers
#     the genuine identity-question phrasing.
#   - "what version of" -- matched an ordinary technical question ("what
#     version of Python/Node should I use") that has nothing to do with
#     this app; "what version are you" already covers the genuine phrasing.
#   - "do you support"  -- matched an opinion/approval question ("do you
#     support this idea/plan") entirely unrelated to app features; "what do
#     you support"/"what features do you support" already cover the
#     genuine capability-question phrasing.
_SELF_DESCRIBE_PHRASES = (
    "what can you do",
    "what do you support",
    "what are your capabilities",
    "what are your limits",
    "what features do you have",
    "what features do you support",
    "what models do you use",
    "which models do you use",
    "which models do you support",
    "what version are you",
    "are you rate limited",
    "what's your budget",
    "what is your budget",
    "how much budget",
    "tell me about yourself",
)


def looks_like_capabilities_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra note."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _SELF_DESCRIBE_PHRASES)


# Which questions get the full module inventory (app/codebase_inventory.py)
# folded into the note, on top of the facts every capabilities answer
# already carries. Narrow for a different reason than
# _SELF_DESCRIBE_PHRASES: not precision about intent, but COST — the
# inventory is ~3,100 tokens, so it rides only on the questions that
# demonstrably go wrong without it, never on "what models do you use".
#
# Every phrase here names the app or addresses it in the second person
# ("you", "your", "yourself", "this app", "the app"). That is the whole
# defence against the failure mode the fact_check phrase-list post-mortem
# found, and it is what keeps a bare "what could be improved" (about the
# user's own code, the overwhelmingly more common question in this app)
# from dragging 3,100 tokens of unrelated module listing into the answer.
#
# "cons and improvements" is the one phrase with no such anchor, kept
# deliberately: it is the exact wording that produced the spreadsheet in
# codebase_inventory.py's docstring, and it has no plausible reading that
# is not a request to critique something. See the trap tests.
_IMPROVEMENT_PHRASES = (
    "what are your weaknesses",
    "what are your limitations",
    "what are your shortcomings",
    "what could you do better",
    "what would you improve",
    "how could you be improved",
    "how can you be improved",
    "how could you be better",
    "how would you improve yourself",
    "improve this app",
    "improve the app",
    "improvements to this app",
    "improvements to the app",
    "weaknesses of this app",
    "what's wrong with this app",
    "what is wrong with this app",
    "cons and improvements",
)


def looks_like_improvement_request(question: str) -> bool:
    """True for a question asking this app to critique ITSELF — the only
    case that earns the module inventory's token cost."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _IMPROVEMENT_PHRASES)


def _model_map() -> dict[str, Any]:
    """Tier + task-category effective models, stripped down to just the
    facts a model answering a user's question needs — not the admin-only
    override/env-var raw values describe_settings() also carries."""
    from .settings import describe_settings

    settings = describe_settings()
    return {
        "tiers": {item["key"]: item["effective_model"] for item in settings["tiers"]},
        "categories": {
            item["category"]: item["effective_model"] for item in settings["categories"]
        },
    }


def _flags() -> dict[str, bool]:
    from .settings import describe_settings

    settings = describe_settings()
    return {item["key"]: item["effective_enabled"] for item in settings["features"]}


def _disabled_features() -> list[dict[str, str]]:
    """Every optional feature that's currently OFF, with its one-line
    purpose — so a model can flag "X would have helped here, but it's
    disabled" instead of just quietly doing without. Deliberately the
    inverse of _flags()'s enabled set: this tool is read-only (see the
    module docstring — capabilities_snapshot() has no real-world side
    effects), so the model can surface a disabled feature but never
    enable one itself; only the owner can, in Settings."""
    from .settings import describe_settings

    settings = describe_settings()
    return [
        {"key": item["key"], "purpose": item["description"]}
        for item in settings["features"]
        if not item["effective_enabled"]
    ]


def _data_policy() -> list[dict[str, str]]:
    """The retention/expiry settings, as [{key, label, effective_value}].

    In the snapshot because leaving them out produced a confident false
    negative: asked what this app lacked, a model reported that share links
    "lack time-bounded expiry" and proposed adding TTLs. SHARE_EXPIRY_DAYS
    has existed all along — it just defaults to blank (never), and nothing
    in the snapshot mentioned it, so the model saw only the word "revocable"
    in the interface description and drew the obvious conclusion.

    Reported as effective VALUES rather than on/off, because unlike a
    feature flag the interesting part is the number: "expiry exists and is
    currently unset" is a fair thing to criticise, where "expiry does not
    exist" is simply wrong. The default being permissive is a real critique
    this lets a model make accurately instead of guessing.
    """
    from .settings import describe_settings

    return [
        {
            "key": item["key"],
            "label": item["label"],
            "effective_value": item["effective_value"],
        }
        for item in describe_settings()["retention"]
    ]


def _limits() -> dict[str, int]:
    """Known per-request/per-conversation limits — a small, deliberately
    curated subset of schemas.py's validation constants, not every internal
    cap (e.g. per-field char limits nobody would ask about).

    Imports app.workflow lazily (inside the function, not at module level):
    workflow.py itself imports from app.orchestrator, and orchestrator.py
    imports this module — a module-level import here would be a circular
    import (see the module docstring's note on lazy imports generally).
    """
    from .schemas import (
        _MAX_CHAT_MESSAGES,
        _MAX_COMPARE_MODELS,
        _MAX_IMPORT_MESSAGES,
        _MAX_INPUT_FILES,
        _MAX_INPUT_IMAGES,
        _MAX_QUESTION_CHARS,
        _MAX_SYSTEM_PROMPT_CHARS,
    )
    from .workflow import max_steps as workflow_max_steps

    return {
        "max_question_chars": _MAX_QUESTION_CHARS,
        "max_attached_images": _MAX_INPUT_IMAGES,
        "max_attached_files": _MAX_INPUT_FILES,
        "max_compare_models": _MAX_COMPARE_MODELS,
        "max_workflow_steps": workflow_max_steps(),
        "max_custom_instructions_chars": _MAX_SYSTEM_PROMPT_CHARS,
        "max_chat_messages_per_compat_request": _MAX_CHAT_MESSAGES,
        "max_import_messages": _MAX_IMPORT_MESSAGES,
    }


def _owner_budget(owner: str | None) -> dict[str, float | None]:
    """This caller's own remaining per-owner daily budget — same computation
    GET /v1/usage already exposes (see app/routers/usage.py), never the live
    global spend total."""
    from .budget import daily_budget_per_owner_usd
    from .database import usage_summary

    limit = daily_budget_per_owner_usd()
    if limit is None:
        return {"daily_budget_per_owner_usd": None, "owner_remaining_usd": None}
    today_usd = float(usage_summary(owner, days=1)["today_usd"])
    return {
        "daily_budget_per_owner_usd": limit,
        "owner_remaining_usd": max(0.0, limit - today_usd),
    }


def capabilities_snapshot(owner: str | None) -> dict[str, Any]:
    """The full self-description JSON: version, how the app is built, what
    subsystems it is built OUT OF, what its interface can do, model map,
    feature flags, known request limits, this caller's own remaining
    per-owner budget, and free-lane quota status — everything
    self_describe()/GET /v1/capabilities return. Every field here is read
    from this app's actual configured state, never invented — including
    `ui`, whose optional clauses are gated on the same live flags
    `disabled_features` is computed from, and `subsystems`, which is parsed
    off the source tree rather than written down (see
    app/codebase_inventory.py).

    `subsystems` is always present here — the JSON has no token cost. It is
    format_note() that decides whether to RENDER it into a prompt."""
    from . import free_tier

    return {
        "version": APP_VERSION,
        "internals": INTERNALS_SUMMARY,
        "subsystems": [dict(entry) for entry in codebase_inventory.subsystems()],
        "ui": _ui_capabilities(),
        "ui_panels": [dict(panel) for panel in codebase_inventory.ui_panels()],
        "models": _model_map(),
        "flags": _flags(),
        "disabled_features": _disabled_features(),
        "limits": _limits(),
        "data_policy": _data_policy(),
        "budget": _owner_budget(owner),
        "free_lane": {
            "enabled": free_tier.enabled(),
            "models": free_tier.status(),
        },
    }


def grounded_question(question: str, note: str) -> str:
    """Re-ask `question` with the verified capability facts supplied as context.

    Used when the model's reply was the tool call and NOTHING else — the
    ordinary shape for a tool-calling turn, since both providers end the turn
    on a `tool_use` block to await a result this codebase never sends back
    (see the module docstring). Folding the note in as the whole answer then
    means the user gets a configuration listing instead of an answer: two
    genuinely different questions ("how is this better than other apps?",
    "what makes it weaker?") came back with the identical dump, which is what
    prompted this.

    So the facts go into the prompt instead of into the reply, and the model
    answers the question the user actually asked, grounded in them. The
    instruction to not simply list them back is the entire point — the dump is
    what we are replacing.
    """
    return (
        f"{question}\n\n"
        "[Verified facts about the app you are embedded in, read from its live "
        "configuration just now. Treat them as ground truth, use only whichever "
        "are relevant, and answer the question above in your own words — do NOT "
        "simply list these back.]\n"
        f"{note}"
    )


def format_note(snapshot: dict[str, Any], include_subsystems: bool = False) -> str:
    """A short, human-readable summary of `snapshot` to append to an
    answer — the identity line plus the handful of facts a "what can you
    do"-style question actually wants, not the full raw JSON.

    `include_subsystems` adds the full module inventory (see
    app/codebase_inventory.py). Off by default because it costs ~3,100
    tokens: callers turn it on only for a question that is asking this app
    to critique itself (see looks_like_improvement_request), where answering
    without it means confidently proposing subsystems that already exist.
    """
    lines = [
        "I'm the assistant embedded in ai-orchestrator, a self-hosted "
        f"multi-provider AI chat app (v{snapshot['version']}). Verified "
        "capabilities (not a guess):",
        f"- {snapshot['internals']}",
        f"- {snapshot['ui']}",
    ]
    if include_subsystems:
        # Straight after `internals`, whose prose summary this is the
        # precise, complete version of — so a model reading top-down has the
        # real inventory before it reaches flags and limits.
        inventory = codebase_inventory.format_lines()
        if inventory:
            lines.append(inventory)
    models = snapshot["models"]["tiers"]
    if models:
        model_bits = ", ".join(f"{tier}: {model}" for tier, model in models.items())
        lines.append(f"- Models — {model_bits}")
    enabled_flags = sorted(key for key, on in snapshot["flags"].items() if on)
    lines.append(
        f"- Enabled optional features — {', '.join(enabled_flags) if enabled_flags else 'none'}"
    )
    disabled = snapshot["disabled_features"]
    if disabled:
        disabled_bits = ", ".join(f"{f['key']} ({f['purpose']})" for f in disabled)
        lines.append(
            "- Available but off — the owner can enable these in Settings — "
            f"{disabled_bits}"
        )
    limits = snapshot["limits"]
    lines.append(
        "- Limits — "
        f"{limits['max_question_chars']:,} chars/question, "
        f"{limits['max_attached_images']} images, "
        f"{limits['max_attached_files']} files per message, "
        # Printed because omitting it read as absent: a model reported that
        # workflow mode could over-plan and proposed adding "hard step
        # ceilings", which WORKFLOW_MAX_STEPS has enforced all along. The
        # snapshot carried it; this line just stopped short of saying so.
        f"{limits['max_workflow_steps']} workflow steps, "
        f"{limits['max_compare_models']} models per comparison"
    )
    policy = snapshot.get("data_policy") or []
    if policy:
        lines.append(
            "- Data policy — "
            + ", ".join(
                f"{item['label']}: {item['effective_value'] or 'unset'}"
                for item in policy
            )
        )
    remaining = snapshot["budget"]["owner_remaining_usd"]
    if remaining is not None:
        lines.append(f"- Your remaining daily budget — ${remaining:,.4f}")
    free_lane = snapshot["free_lane"]
    if free_lane["enabled"] and free_lane["models"]:
        lane_bits = ", ".join(
            f"{m['model']} ({m['remaining']}/{m['quota']} left today)"
            for m in free_lane["models"]
        )
        lines.append(f"- Free-lane models — {lane_bits}")
    return "\n".join(lines)
