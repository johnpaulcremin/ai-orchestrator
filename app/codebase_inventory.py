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

# Long enough to keep a real first sentence intact (most in this package run
# 100-150 chars), short enough that ~75 modules stay affordable to include.
# Truncation is marked with an ellipsis rather than being silent.
_MAX_SUMMARY_CHARS = 170

# A first sentence this short ("Weekly self-report.") carries no information,
# so the next one is appended before the length cap applies.
_MIN_FIRST_SENTENCE_CHARS = 40

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


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
    the obvious choice — it was the first cut here, and it was wrong. Sixteen
    modules in this package carry no docstring, and they are not the
    peripheral ones: database, routing, cache, providers, settings, auth,
    security, ratelimit. Skipping them would have left the inventory silent
    on precisely the subsystems the spreadsheet in this module's docstring
    got wrong, which is a strange way to fix that spreadsheet. A bare
    `ratelimit` or `security_headers` still answers the only question being
    asked of this list — does it exist — and it costs a handful of tokens
    to say so.

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
