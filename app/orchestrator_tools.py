"""Optional-tool plumbing for an answer call: the propose_action, image
generation, and code_interpreter tool definitions, the settings gates that
decide whether each is offered, and _build_tools, which collapses whichever
are active into the single `tools` kwarg the Responses API accepts."""

from __future__ import annotations

import os
import re
from typing import Any

from .actions import ACTION_TOOL_DESCRIPTION, action_input_schema
from .math_solve import MATH_SOLVE_TOOL_DESCRIPTION, math_solve_input_schema
from .orchestrator_extract import _WEB_SEARCH_TOOL
from .self_describe import (
    APP_CAPABILITIES_TOOL_DESCRIPTION,
    app_capabilities_input_schema,
)
from .settings import bool_setting
from .usage import estimate_image_cost


def _build_action_tool() -> dict[str, Any]:
    """The propose_action function tool, OpenAI Responses API shape. Its
    `action` field is restricted to an enum of the operator's actual
    configured named routes (see action_input_schema/actions.named_webhooks)
    when any exist — so the model can only ever propose an action type that
    has somewhere real to go, instead of inventing a name that silently
    falls through to the catch-all webhook (or nowhere, if there isn't one).
    Falls back to a freeform string when ACTIONS_WEBHOOKS isn't set. See
    providers._anthropic_action_tool for the Anthropic-shaped equivalent —
    same description and input schema, different wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "propose_action",
                "description": ACTION_TOOL_DESCRIPTION,
                "parameters": action_input_schema(),
                "strict": False,
            }
        ]
    }


def _image_generation_enabled() -> bool:
    """Opt-in: IMAGE_GENERATION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) turns on image
    generation.

    Which code path is used depends on _image_generation_provider() AND on
    which model the router picked to answer: the OpenAI backend offers a
    hosted tool and lets the model decide when to call it (same as
    propose_action), but only an OpenAI model can be offered one. Every other
    combination — the Gemini backend, or the OpenAI backend on a turn the
    router sent to Claude/Ollama/any LiteLLM model — falls back to a
    standalone image call gated by _looks_like_image_request. Off by default
    either way.
    """
    return bool_setting("IMAGE_GENERATION", False)


def _image_generation_model() -> str:
    return (os.getenv("IMAGE_GENERATION_MODEL") or "").strip() or "gpt-image-1"


def _image_generation_provider() -> str:
    """ "openai" (the built-in Responses API tool) or "gemini" (a standalone
    LiteLLM image_generation call, since Gemini/Imagen has no equivalent of a
    tool the chat model can call itself) — selected by IMAGE_GENERATION_MODEL's
    prefix, the same "prefix picks the provider" convention used everywhere
    else in this app (OPENAI_MODEL_FAST=gemini/... routes through LiteLLM too).
    """
    return (
        "gemini"
        if _image_generation_model().strip().lower().startswith("gemini/")
        else "openai"
    )


_IMAGE_GENERATION_QUALITIES = {"low", "medium", "high", "auto"}


def _image_generation_quality() -> str:
    # Default "high": once an operator opts in, best-effort quality is the
    # point — cost-sensitive deployments can override this down.
    raw = (os.getenv("IMAGE_GENERATION_QUALITY") or "high").strip().lower()
    return raw if raw in _IMAGE_GENERATION_QUALITIES else "high"


def _image_generation_size() -> str:
    return (os.getenv("IMAGE_GENERATION_SIZE") or "").strip() or "auto"


def _worst_case_image_cost(images_wanted: bool, standalone_image_wanted: bool) -> float:
    """Pre-dispatch budget estimate for this call's possible image generation.

    Neither gate guarantees an image actually gets generated (the OpenAI tool
    is only offered, not forced; the standalone path always requests exactly
    one), but the budget gate already prices every call at its worst case (the
    full output token budget, even if the model uses less) — assuming one image
    here when either path is live is the same philosophy, not a new one.
    """
    if not (images_wanted or standalone_image_wanted):
        return 0.0
    return estimate_image_cost(1, _image_generation_quality()) or 0.0


def _build_image_generation_tool() -> dict[str, Any]:
    return {
        "type": "image_generation",
        "model": _image_generation_model(),
        "quality": _image_generation_quality(),
        "size": _image_generation_size(),
    }


# The trigger for the standalone image-generation call (see
# _image_generation_enabled) — the path taken whenever the answering model
# cannot host an image tool itself, so something other than the model has to
# decide when an image is wanted.
#
# This was a flat list of literal phrases ("create an image", "draw a", ...).
# Two grammars replace it, because the flat list was simultaneously too narrow
# and too broad, and a longer flat list could only fix the first half:
#
#   Too narrow: it enumerated verb/article pairs, so it turned on "create an
#   image" and off "produce an image", on "make a picture" and off "make me a
#   quick picture", and had no entry at all for the nouns people actually ask
#   for — diagram, mockup, poster, icon, illustration. Every gap read to the
#   user as a capability the app lacked.
#
#   Too broad: "draw a"/"draw an"/"draw me" matched on the verb alone, so
#   "draw a conclusion", "draw an analogy" and "draw the line" each bought an
#   image. Widening the verbs without fixing that would have multiplied it.
#
# Still deliberately high-precision, and still biased toward missing a request
# over over-triggering: a false positive spends real money on an image nobody
# asked for.

# Verbs that ARE a request for a picture on their own — whatever follows is
# the subject of the drawing ("draw me a cat"), so no picture-noun is needed.
# "redraw" earns its place from a live miss: "Can you redraw yourself using
# similar looking logo's..." matched nothing — \bdraw\b cannot see the verb
# inside "redraw" — so the turn got no image, no ground-truth block, and the
# model, guessing, told the user image generation was switched off when it
# was on. A re- prefix does not change what the verb asks for.
_PICTURE_VERBS = (
    "draw",
    "redraw",
    "re-draw",
    "sketch",
    "paint",
    "illustrate",
    "doodle",
)

# ...except where English uses those same verbs for something abstract. Only
# consulted for the verb-alone rule; "paint a picture" is a picture either way.
#
# Two checks, because the abstraction sits in two different places. It is
# usually the verb's object ("draw a CONCLUSION"), caught by _ABSTRACT_OBJECTS
# in the head position. But English fronts that object freely — "what
# CONCLUSIONS do you draw a year later" leaves the head word an innocent
# "year" — so the same set is also checked against everything BEFORE the verb.
_ABSTRACT_OBJECTS = frozenset(
    {
        "conclusion",
        "conclusions",
        "comparison",
        "comparisons",
        "analogy",
        "analogies",
        "parallel",
        "parallels",
        "distinction",
        "distinctions",
        "inference",
        "inferences",
        "line",
        "lines",
        "blank",
        "attention",
        "breath",
        "straw",
        "straws",
        "inspiration",
        "criticism",
        # The electoral idiom "redraw" drags in with it: "redraw the district
        # boundaries" is politics, not a picture. Same judgement as "line" —
        # a decorative "draw a border" request is sacrificed to avoid paying
        # for an image nobody asked for, the bias this whole list runs on.
        "boundary",
        "boundaries",
        "district",
        "districts",
    }
)

# "draw up a plan", "draw on experience", "draw out the argument", "what do
# you draw from this" — a preposition in the head position is never a thing
# anyone can draw, and it is the other half of how these verbs go abstract.
_NON_SUBJECT_HEADS = frozenset(
    {
        "up",
        "on",
        "upon",
        "out",
        "in",
        "into",
        "from",
        "off",
        "over",
        "under",
        "down",
        "near",
        "between",
        "against",
        "with",
        "without",
        "before",
        "after",
        "alongside",
        "toward",
        "towards",
    }
)

# Verbs that mean "make a picture" ONLY when they take a picture-noun:
# "create a mockup" yes, "create a function" no.
_MAKER_VERBS = (
    "generate",
    "create",
    "make",
    "produce",
    "render",
    "design",
    "show",
    "give",
    "visualise",
    "visualize",
    "whip up",
    "knock up",
)

# Deliberately excludes chart/graph/plot: those are data visualisations, and
# this app answers them properly through code execution (a real chart from
# real numbers) rather than by asking an image model to imagine one.
_PICTURE_NOUNS = (
    "image",
    "images",
    "imagine",
    "picture",
    "pictures",
    "photo",
    "photos",
    "photograph",
    "photographs",
    "drawing",
    "drawings",
    "illustration",
    "illustrations",
    "diagram",
    "diagrams",
    "artwork",
    "art",
    "logo",
    "logos",
    "icon",
    "icons",
    "poster",
    "posters",
    "banner",
    "banners",
    "sketch",
    "sketches",
    "painting",
    "paintings",
    "graphic",
    "graphics",
    "visual",
    "visuals",
    "visualisation",
    "visualization",
    "mockup",
    "mock-up",
    "avatar",
    "portrait",
    "wallpaper",
    "infographic",
    "flowchart",
    "comic",
    "cartoon",
    "sticker",
    "headshot",
    "render",
)
# "imagine" is in that list on purpose: as a NOUN, behind an article, it is
# only ever the misspelling of "image" that prompted this widening. As a verb
# ("imagine a world where...") it never reaches this position, so it costs no
# precision to accept.


def _alternation(words: tuple[str, ...]) -> str:
    """Longest-first so "mock-up" cannot be half-matched as "mock"."""
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_RECIPIENT = r"(?:(?:me|us|for me|for us)\s+)?"
_ARTICLE = r"(?:(?:a|an|the|some|this|that|another|one|my|your|its)\s+)?"
# Up to three adjectives between the article and the noun, so "generate a
# high resolution image" lands but "create a function that returns an image"
# (four) does not.
_MODIFIERS = r"(?:[\w'-]+\s+){0,3}?"

_PICTURE_NOUN_RE = re.compile(
    rf"\b(?:{_alternation(_PICTURE_VERBS + _MAKER_VERBS)})\s+"
    rf"{_RECIPIENT}{_ARTICLE}{_MODIFIERS}"
    rf"(?:{_alternation(_PICTURE_NOUNS)})\b"
)

# The head word is captured rather than excluded with a lookahead: every group
# before it is optional, so a lookahead would just backtrack into the article
# ("draw a conclusion" -> head "a", not abstract, match) and never fire.
_PICTURE_VERB_RE = re.compile(
    rf"\b(?:{_alternation(_PICTURE_VERBS)})\s+"
    rf"{_RECIPIENT}{_ARTICLE}"
    r"(?P<head>[\w'-]+)"
)

_WORD_RE = re.compile(r"[\w'-]+")


# Drawings whose VALUE is structural: boxes, arrows, labels, and the
# relationships between them. Code execution renders these properly — real
# geometry, real text — while an image model produces an impression of one,
# with the labels garbled, which is the opposite of what a diagram is for.
#
# The picture-noun list already excludes chart/graph/plot for this reason.
# This list is that judgement finished: it also covers the diagram nouns that
# WERE in the list, and it applies to the verb rule too, which is how "draw
# me a chart" reached the image path despite the exclusion.
_DRAWN_BY_CODE_NOUNS = (
    "diagram",
    "diagrams",
    "flowchart",
    "flow chart",
    "flowcharts",
    "schematic",
    "schematics",
    "wireframe",
    "wireframes",
    "org chart",
    "mind map",
    "sequence diagram",
    "architecture",
    "uml",
    "chart",
    "charts",
    "graph",
    "graphs",
    "plot",
    "plots",
)


def prefers_drawn_by_code(question: str) -> bool:
    """Whether this request names a drawing better produced by running code
    than by an image model. Only consulted when code execution is actually
    available to the answering model — see orchestrator._tool_flags_for."""
    text = " ".join((question or "").lower().split())
    return any(
        re.search(rf"\b{re.escape(noun)}\b", text) for noun in _DRAWN_BY_CODE_NOUNS
    )


def _looks_like_image_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra paid call."""
    text = " ".join((question or "").lower().split())
    if _PICTURE_NOUN_RE.search(text):
        return True
    for match in _PICTURE_VERB_RE.finditer(text):
        head = match.group("head")
        if head in _ABSTRACT_OBJECTS or head in _NON_SUBJECT_HEADS:
            continue
        before = set(_WORD_RE.findall(text[: match.start()]))
        if before & _ABSTRACT_OBJECTS:
            continue
        return True
    return False


# A file the user expects to receive, asked for in an ORDINARY single ask.
# A workflow gets this judgement from its planner (`produces_artefact`), which
# is why the ceiling fix that followed a truncated spreadsheet only ever
# reached workflow steps — a plain "put this into an Excel document" keeps its
# category's prose-sized tier cap and is cut off exactly the same way.
#
# Nouns, not verbs: "spreadsheet"/"xlsx" identify the deliverable however it
# is asked for ("make", "put this into", "can you build"), whereas the picture
# grammar above needs a verb rule at all because "draw" has no noun that
# survives paraphrase. Same bias as that rule, for the same reason — a false
# positive raises a ceiling (and the reservation behind it) on a prose answer
# that never needed it.
_ARTEFACT_REQUEST_PHRASES = (
    "spreadsheet",
    "xlsx",
    "excel document",
    "excel file",
    "excel workbook",
    "csv file",
    ".csv",
    "word document",
    "docx",
    "pdf file",
    "a pdf",
    "downloadable file",
    "as a file",
    "into a file",
)


def artefact_file_instructions(artefact: str = "") -> list[str]:
    """The rules a reply must follow when its whole purpose is to hand back a
    FILE. One list, used by both paths that need it.

    Lives here rather than in app/workflow.py — where every one of these rules
    was learned — because the plain single-ask path needs the identical rules
    and a second copy would drift from the first the moment either was
    corrected. Each paragraph below records a specific observed failure; see
    the comments, which came with the rules.

    `artefact` names the file when the caller knows it (a workflow step gets
    the name from its planner). A plain ask does not know it, so the phrasing
    falls back to "the file the request asks for" — the demand is identical
    either way; only the noun changes.
    """
    named = artefact or "the file the request asks for"
    lines: list[str] = []
    # An artefact step's entire purpose is the FILE. Saying so explicitly
    # matters: with code execution available but nothing asking for a file,
    # a model reliably answers in prose and never calls the tool — which is
    # exactly how a three-artefact request came back as a markdown table
    # and ASCII bars, and how a plain "make the spreadsheet" spent an entire
    # 8,000-token ceiling narrating the workbook it was about to build.
    lines.append(
        f"You must PRODUCE A REAL FILE: {named}. Write and run "
        "code to generate it and save it to disk, so it comes back as an "
        "actual downloadable file. Do NOT print the contents as a markdown "
        "table, ASCII art, or a code block instead — a described file is a "
        "failure. Keep any accompanying prose to one sentence."
    )
    # A caveat row is not data. A live run appended
    # `Note,All listed costs are illustrative examples, not live billing
    # data,` under a three-column header: the unquoted commas split it into
    # extra fields, so the file no longer parses under a strict CSV reader
    # and openpyxl/pandas read a ragged trailing row. The caveat itself was
    # worth saying — just not in the table.
    lines.append(
        "If the file is tabular (.csv/.xlsx), it must contain ONLY the "
        "data: exactly one header row, then data rows, every row with the "
        "same number of columns as the header. Never append a note, "
        "caveat, disclaimer, source line, or total as an extra row, and "
        "never leave a comma unquoted inside a field. Anything you want to "
        "say about the data belongs in your one sentence of prose, not in "
        "the file."
    )
    # A blank cell is not a ragged row, so the width rule above lets it
    # through — and it reads to whoever opens the file as an omission
    # rather than a fact about the data. Observed live: the last row of a
    # generated .xlsx had its final column empty, with nothing saying
    # whether that meant "none" or "ran out".
    #
    # The "never invent" half is the load-bearing half. Told only to fill
    # every cell, a model will happily manufacture a plausible value for
    # one it does not have, which turns a visible gap into an invisible
    # fabrication — strictly worse, and the exact trade every other rule
    # here refuses to make.
    lines.append(
        "Every cell must carry a value. Where one genuinely does not "
        'apply or you do not have it, write "n/a" — do NOT leave it '
        "blank, and do NOT invent a value to fill it. A blank cell reads "
        "as something forgotten; a made-up one is worse than either."
    )
    return lines


def _looks_like_artefact_request(question: str) -> bool:
    """Whether this ask wants a FILE handed back, not prose about one.

    Gates the output-ceiling raise on the ordinary ask path (see
    orchestrator._apply_code_execution_override): a file-producing reply emits
    code carrying every row of the data, which does not fit in a ceiling sized
    for text. Errs toward missing one, same as _looks_like_image_request.
    """
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _ARTEFACT_REQUEST_PHRASES)


def _code_execution_enabled() -> bool:
    """Opt-in: CODE_EXECUTION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) lets the model run
    Python via OpenAI's hosted code_interpreter tool — a sandboxed container
    in OpenAI's own cloud, never on this machine, same trust boundary as
    web_search/image_generation. The model decides for itself when running
    code would help (verifying a calculation, testing a snippet), same as
    propose_action/image_generation. Off by default.
    """
    return bool_setting("CODE_EXECUTION", False)


_CODE_INTERPRETER_TOOL: dict[str, Any] = {
    "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]
}


def _math_solve_enabled() -> bool:
    """Opt-in: MATH_SOLVE=true (env, or a saved Settings override — same
    override > env > default chain as any other feature flag). Off by
    default, same as every other optional tool here. Lives here rather than
    in app/math_solve.py itself to avoid a circular import: settings.py
    imports providers.py (for key_env_for/provider_of), and providers.py
    needs math_solve.py's tool description/schema for the Anthropic tool
    definition — so math_solve.py itself must not import settings.py."""
    return bool_setting("MATH_SOLVE", False)


def _build_math_solve_tool() -> dict[str, Any]:
    """The math_solve function tool, OpenAI Responses API shape. Unlike
    propose_action, a call to this is executed immediately (no user
    confirmation needed — see app/math_solve.py's module docstring). See
    providers._anthropic_math_solve_tool for the Anthropic-shaped
    equivalent — same description and input schema, different wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "math_solve",
                "description": MATH_SOLVE_TOOL_DESCRIPTION,
                "parameters": math_solve_input_schema(),
                "strict": False,
            }
        ]
    }


def _build_self_describe_tool() -> dict[str, Any]:
    """The app_capabilities function tool, OpenAI Responses API shape. Same
    "executed immediately, no confirmation" reasoning as math_solve (see
    app/self_describe.py's module docstring). See
    providers._anthropic_self_describe_tool for the Anthropic-shaped
    equivalent — same description and (empty) input schema, different
    wrapper.
    """
    return {
        "tools": [
            {
                "type": "function",
                "name": "app_capabilities",
                "description": APP_CAPABILITIES_TOOL_DESCRIPTION,
                "parameters": app_capabilities_input_schema(),
                "strict": False,
            }
        ]
    }


def _build_tools(
    web_search: bool,
    actions: bool,
    images: bool = False,
    code_execution: bool = False,
    math_solve: bool = False,
    capabilities: bool = False,
) -> dict[str, Any]:
    """The combined `tools` kwarg for however many optional tools are active.

    web_search, actions, images, code_execution, math_solve, and
    capabilities are independent features that all just add an entry to the
    SAME `tools` list the Responses API accepts — collapsing them here keeps
    the retry ladder below a single "has tools or not" dimension instead of
    a combinatorial one.
    """
    tools: list[dict[str, Any]] = []
    if web_search:
        tools.extend(_WEB_SEARCH_TOOL["tools"])
    if actions:
        tools.extend(_build_action_tool()["tools"])
    if images:
        tools.append(_build_image_generation_tool())
    if code_execution:
        tools.extend(_CODE_INTERPRETER_TOOL["tools"])
    if math_solve:
        tools.extend(_build_math_solve_tool()["tools"])
    if capabilities:
        tools.extend(_build_self_describe_tool()["tools"])
    return {"tools": tools} if tools else {}
