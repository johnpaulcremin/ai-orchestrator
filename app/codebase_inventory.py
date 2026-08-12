"""Codebase inventory: what this app is ACTUALLY built out of, read off its
own source tree at runtime and handed to a model that has been asked to
critique it.

Exists because of a real failure. Asked for "cons and improvements", the app
produced a competent-looking spreadsheet that listed automated backups,
retention policies, rate limiting, security headers and provider health
checks as things it lacked — every one of which is a module sitting in this
package (db_backup.py, retention.py, ratelimit.py, security_headers.py,
local_health.py). The model was not lying; it had nothing to go on.
self_describe.py's INTERNALS_SUMMARY is a static paragraph about the
architecture, and _flags() reports which optional FEATURES are switched on,
but neither says what code exists — so "what's missing?" was answered from
priors about a generic self-hosted chat app.

So the inventory is derived, never written down: every module's own
docstring first sentence, extracted with `ast` from the files on disk. A
subsystem added tomorrow appears here the same day with no second place to
update, which is the entire point — a hand-maintained list would drift back
into exactly the wrong answer it exists to prevent, and would do it
silently.

It then happened AGAIN, one stack over, and the same fix applies: a later
critique reported the app had no usage analytics and proposed building the
daily-spend and per-model charts the Usage panel has shipped for months,
because self_describe.py's hand-written paragraph about the interface never
mentioned that panel. See ui_panels(), which reads the frontend's own <h2>
panel titles and <h3> section headings — the interface's ground truth, and
the one description of it that cannot fall out of date.

Parsed, never imported: `ast.parse` reads the source as text, so building
the inventory triggers no module-level side effects, no circular imports
(this package's import graph is dense — see self_describe.py's lazy-import
note), and no cost. Cached for process lifetime since the source tree does
not change under a running server.

NOT free to include in a prompt: the full listing is a few thousand
characters, which is why self_describe.format_note() only renders it for a
question that is actually asking for a critique (see
looks_like_improvement_request) rather than on every capabilities note. The
JSON snapshot (GET /v1/capabilities) always carries it — there is no token
cost there.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_FRONTEND_ROOT = _PACKAGE_ROOT.parent / "frontend" / "src"

# Long enough to keep a real first sentence intact (most in this package run
# 100-150 chars), short enough that ~80 modules stay affordable to include.
# Truncation is marked with an ellipsis rather than being silent.
#
# THE RULE THIS IMPLIES, for anyone writing a module docstring here: the
# first sentence is not an opening, it is the ENTIRE description for the only
# reader who matters to this file — a model deciding whether this app already
# does something. So it must state what the module GUARANTEES, not merely
# what it is, and must do it inside this cap.
#
# Learned the expensive way, three times. cache.py's first sentence said
# whole answers are keyed by the question; its invalidation rules were in
# paragraph two, and a critique twice proposed adding the versioned
# invalidation that has always existed. self_report.py's said the app writes
# a digest about itself, and a critique proposed surfacing retry cost and
# fallback trends the digest already prints. workflow.py's named the mode and
# its enum, and a critique proposed the step ceiling max_steps() has always
# clamped. In each case the docstring documented the fact correctly and the
# summary never reached it. Raising the cap would not have helped — a
# paragraph nobody reads is not made better by being longer.
_MAX_SUMMARY_CHARS = 170

# A first sentence this short ("Weekly self-report.") carries no information,
# so the next one is appended before the length cap applies.
_MIN_FIRST_SENTENCE_CHARS = 40

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# The frontend has no docstring equivalent, so its inventory is read from the
# thing that IS ground truth about the interface: the headings a user actually
# sees. Every modal panel in frontend/src/*.tsx titles itself with an <h2> and
# names its sections with <h3>, so "Usage → Last N days / By model / Quality"
# falls straight out of the markup with nothing to keep in sync by hand.
_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_JSX_EXPRESSION = re.compile(r"\{[^{}]*\}")
_TAG = re.compile(r"<[^>]*>")


def _summarize(doc: str) -> str:
    """First sentence (or two, if the first is a bare label) of a module
    docstring, whitespace-collapsed and length-capped."""
    text = " ".join(doc.split())
    if not text:
        return ""
    sentences = _SENTENCE_SPLIT.split(text)
    summary = sentences[0]
    if len(summary) < _MIN_FIRST_SENTENCE_CHARS and len(sentences) > 1:
        summary = f"{summary} {sentences[1]}"
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    return summary


def _module_name(path: Path) -> str:
    """Dotted name relative to this package: `self_report`,
    `routers.settings`, `routers.messages.ask`."""
    relative = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    return ".".join(relative.parts)


def _read_docstring(path: Path) -> str | None:
    """The module's docstring, or None if it has none or cannot be parsed.

    encoding="utf-8" is not optional: this package's sources are full of
    em-dashes and emoji, and on Windows the platform default (cp1252) raises
    on them — which would empty the inventory on exactly the machine this app
    is developed on.
    """
    try:
        source = path.read_text(encoding="utf-8")
        return ast.get_docstring(ast.parse(source))
    except (OSError, SyntaxError, ValueError):
        # A source tree that cannot be read is not a reason to fail a request:
        # the caller degrades to no inventory, same best-effort contract as
        # every other note self_describe.py composes.
        return None


@lru_cache(maxsize=1)
def subsystems() -> tuple[dict[str, str], ...]:
    """EVERY module in this package (bar `__init__.py`), as
    ({"module": ..., "summary": ...}, ...) sorted by module name, with
    `summary` empty for a module that has no docstring.

    Undocumented modules are listed bare rather than skipped, which is not
    the obvious choice — it was the first cut here, and it was wrong.
    Skipping them would leave the inventory silent on precisely the
    subsystems the critiques keep getting wrong, which is a strange way to
    fix those critiques. A bare `ratelimit` still answers the only question
    being asked of this list — does it exist — and costs a handful of tokens
    to say so.

    Bare is nonetheless the weak case, and it cost something real. Sixteen
    modules carried no docstring when this was written, including the least
    peripheral ones (database, routing, cache, providers, settings, auth,
    security), so the model got the bare word `cache` and nothing else — and
    a later critique reported that the semantic cache could serve stale
    answers after a document change, which cache.library_generation has
    prevented all along in BOTH caches. Existence was never the gap; behaviour
    was. Those sixteen are documented now, and
    test_no_module_is_listed_bare keeps it that way.

    Returns () when the source tree is not readable (an
    installed-without-sources deployment), degrading the note to the
    behaviour that existed before this module.
    """
    entries: list[dict[str, str]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        doc = _read_docstring(path)
        entries.append(
            {
                "module": _module_name(path),
                "summary": _summarize(doc) if doc else "",
            }
        )
    return tuple(sorted(entries, key=lambda entry: entry["module"]))


def _heading_text(raw: str) -> str:
    """A JSX heading's user-visible text. An interpolation becomes "N", since
    nearly every one here is a count ("Last {data.days} days" → "Last N
    days"); a heading that is NOTHING but an interpolation reduces to "N" and
    is dropped by the caller, which is how the main view's conversation-title
    <h2> stays out of the panel list."""
    text = _JSX_EXPRESSION.sub("N", raw)
    text = _TAG.sub("", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def ui_panels() -> tuple[dict[str, object], ...]:
    """Every modal panel the frontend can open, as ({"component": ...,
    "panel": ..., "sections": [...]}, ...) sorted by panel name.

    A file counts as a panel only if it titles itself with an <h2> — that is
    the crisp definition of "something the user can open", and it is why
    App.tsx (whose <h2> is the selected conversation's title) and the
    non-modal pieces like MessageList/Sidebar are absent rather than
    listed with no title.

    This exists because the hand-written UI paragraph in self_describe.py
    omitted the Usage panel entirely, and a model asked to critique the app
    duly reported that it had no analytics dashboard and proposed building
    one — daily spend, model mix — that has shipped for months. Same drift a
    hand-maintained list always has, one stack over.

    Known limit, stated rather than left to be discovered: a component
    rendered INSIDE another panel (Users.tsx within Model settings) has no
    <h2> of its own, so its sections are absent — which of the two files a
    heading ends up under is only knowable at runtime, and guessing it from
    imports would be a worse lie than the omission.
    """
    if not _FRONTEND_ROOT.is_dir():
        return ()
    panels: list[dict[str, object]] = []
    for path in sorted(_FRONTEND_ROOT.glob("*.tsx")):
        if path.name.endswith(".test.tsx"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        titles = [
            t for t in (_heading_text(m) for m in _H2.findall(source)) if t != "N"
        ]
        if not titles:
            continue
        sections = [
            s for s in (_heading_text(m) for m in _H3.findall(source)) if s != "N"
        ]
        panels.append(
            {
                "component": path.stem,
                "panel": titles[0],
                "sections": sorted(set(sections)),
            }
        )
    return tuple(sorted(panels, key=lambda panel: str(panel["panel"])))


# Static aria-label/title values — the accessibility layer is ground truth
# for CONTROLS the way headings are for panels. Only plain string attributes:
# a JSX-expression label ({`Bookmark ${...}`}) is per-item detail no summary
# needs. The {} exclusion inside the value guard drops those; 4+ chars drops
# icon-only fragments.
_STATIC_LABEL_RE = re.compile(r'(?:aria-label|title)="([^"{}]{4,})"')

# Long tooltip titles in this codebase are mini-essays; the inventory only
# needs the clause that names the capability.
_MAX_LABEL_CHARS = 90


@lru_cache(maxsize=1)
def ui_controls() -> tuple[dict[str, object], ...]:
    """Main-view interactive controls, as ({"component", "labels": [...]},
    ...) — every static aria-label/title in the frontend files that are NOT
    panels, sorted by component.

    The complement of ui_panels(), by the same derivation discipline: a
    panel describes itself through its headings; everything else (App's
    header controls, the composer, the message toolbar, the sidebar)
    describes itself through the labels its controls already carry for
    accessibility. App.tsx is included even though it has an <h2> — that
    heading is the conversation title, which is why ui_panels drops it, and
    App is precisely where the per-conversation model pin lives.

    Exists because of the third occurrence of the session's one failure
    genus: a critique proposed "per-conversation model pins" and "pre-flight
    cost estimates in the UI" — both shipped, both living in main-view
    chrome that neither the module inventory (backend only) nor the panel
    inventory (<h2> files only) could see.
    """
    if not _FRONTEND_ROOT.is_dir():
        return ()
    panel_components = {str(p["component"]) for p in ui_panels()}
    controls: list[dict[str, object]] = []
    for path in sorted(_FRONTEND_ROOT.glob("*.tsx")):
        if path.name.endswith(".test.tsx") or path.stem in panel_components:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        labels = sorted(
            {
                _SENTENCE_SPLIT.split(" ".join(m.split()))[0][:_MAX_LABEL_CHARS]
                for m in _STATIC_LABEL_RE.findall(source)
            }
        )
        if labels:
            controls.append({"component": path.stem, "labels": labels})
    return tuple(controls)


def format_controls_lines() -> str:
    """The controls inventory as one line per component, or "" without
    frontend sources. Rides with the module inventory under the same
    self-critique gate (see self_describe.format_note): ~500 tokens is not
    worth every capabilities note, but a critique that cannot see the
    composer's cost preview will propose building it."""
    controls = ui_controls()
    if not controls:
        return ""
    lines = [
        "In-view controls, read from the interface's own accessibility "
        "labels (panels above describe themselves; these are the main-view "
        "affordances — assume a labelled control is a shipped feature):"
    ]
    for entry in controls:
        labels = entry["labels"]
        assert isinstance(labels, list)
        lines.append(f"- {entry['component']}: " + "; ".join(labels))
    return "\n".join(lines)


def format_ui_lines() -> str:
    """The panel inventory as one dense clause per panel, or "" when the
    frontend sources are not present.

    Cheap enough (~150 tokens) to ride on every capabilities note rather than
    being gated like the module inventory — it is replacing a prose claim
    about the interface that was already being sent and was already wrong.
    """
    panels = ui_panels()
    if not panels:
        return ""
    bits = []
    for panel in panels:
        sections = panel["sections"]
        if isinstance(sections, list) and sections:
            bits.append(f"{panel['panel']} ({', '.join(str(s) for s in sections)})")
        else:
            bits.append(str(panel["panel"]))
    return (
        "Panels the user can open, read from the frontend's own headings just "
        f"now: {'; '.join(bits)}."
    )


def format_lines() -> str:
    """The inventory as markdown bullets for
    self_describe.format_note()/grounded_question(), or "" when empty.

    The framing sentence is doing real work: without "already implemented",
    a bare module list reads as neutral context, and the failure this module
    exists to fix was a model treating implemented subsystems as absent.
    """
    entries = subsystems()
    if not entries:
        return ""
    header = (
        f"- Subsystems ALREADY IMPLEMENTED in this codebase ({len(entries)} "
        "modules, read from the source tree just now — do not propose any of "
        "these as new work; critique what they do or do not cover instead):"
    )
    bullets = [
        f"  - `{e['module']}` — {e['summary']}"
        if e["summary"]
        else f"  - `{e['module']}`"
        for e in entries
    ]
    return "\n".join([header, *bullets])
