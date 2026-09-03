"""Codebase inventory (app/codebase_inventory.py) and the self-critique gate
that decides when it rides along (app/self_describe.py).

The regression under all of it is concrete: asked for "cons and
improvements", this app produced a spreadsheet listing automated backups,
retention policies, rate limiting, security headers and provider health
checks as MISSING — every one of them a module in this package. So the
tests below pin, end to end, that a self-critique question now reaches the
model with the real module list in hand, and that an ordinary question does
not pay the ~3,100 tokens for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.orchestrator as orchestrator
from app import codebase_inventory, self_describe
from app.orchestrator import run_orchestrator
from app.schemas import AskRequest, Mode

# The five the spreadsheet got wrong, named here rather than in each test so
# a future reader sees exactly which failure this file exists for.
_MISREPORTED_AS_MISSING = (
    "db_backup",
    "retention",
    "ratelimit",
    "security_headers",
    "local_health",
)


@pytest.fixture
def clear_inventory_cache():
    """subsystems()/ui_panels() are lru_cached for process lifetime (the
    source tree does not change under a running server). Any test that
    repoints _PACKAGE_ROOT/_FRONTEND_ROOT must clear them on the way in AND
    out, or it leaks a bogus inventory into every later test in the
    session."""
    codebase_inventory.subsystems.cache_clear()
    codebase_inventory.ui_panels.cache_clear()
    codebase_inventory.ui_controls.cache_clear()
    yield
    codebase_inventory.subsystems.cache_clear()
    codebase_inventory.ui_panels.cache_clear()
    codebase_inventory.ui_controls.cache_clear()


# --- the inventory itself -----------------------------------------------------


def test_inventory_lists_the_modules_the_spreadsheet_called_missing() -> None:
    """The whole point, stated once: these exist, and the inventory says so."""
    modules = {entry["module"] for entry in codebase_inventory.subsystems()}
    for name in _MISREPORTED_AS_MISSING:
        assert name in modules


def test_inventory_covers_routers_too() -> None:
    modules = {entry["module"] for entry in codebase_inventory.subsystems()}
    assert "routers.settings" in modules
    assert "routers.messages.ask" in modules


def test_inventory_skips_dunder_init() -> None:
    modules = {entry["module"] for entry in codebase_inventory.subsystems()}
    assert not any(name.endswith("__init__") for name in modules)


def test_inventory_lists_an_undocumented_module_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    """A module with no docstring is still listed, bare, rather than skipped.

    Uses a synthetic package rather than a real undocumented module, because
    there are no longer any — see
    test_every_module_carrying_a_critiqued_subsystem_is_documented. The
    POLICY still matters: the next module added without a docstring must
    appear here anyway, since a bare `ratelimit` still answers the only
    question the inventory is asked (does this exist), and silently omitting
    it would leave the listing quiet about exactly the kind of subsystem the
    critiques keep getting wrong.
    """
    (tmp_path / "documented.py").write_text('"""Does a thing."""\n', encoding="utf-8")
    (tmp_path / "silent.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(codebase_inventory, "_PACKAGE_ROOT", tmp_path)
    entries = {e["module"]: e["summary"] for e in codebase_inventory.subsystems()}
    assert entries["silent"] == ""
    assert entries["documented"] == "Does a thing."


def test_every_module_carrying_a_critiqued_subsystem_is_documented() -> None:
    """The second-order failure, pinned. The inventory closed the "does this
    module exist" gap and left "what does it do" open: `cache.py` had no
    docstring, so it was listed as the bare word `cache`, and a critique duly
    reported that the semantic cache could serve stale answers after a
    document change — which cache.library_generation has prevented all along,
    in both caches. Every module below is one a critique has reached for."""
    entries = {e["module"]: e["summary"] for e in codebase_inventory.subsystems()}
    for name in (
        "cache",
        "semantic_cache",
        "database",
        "routing",
        "auth",
        "security",
        "ratelimit",
        "settings",
        "providers",
        "usage",
        "schemas",
    ):
        assert entries[name], f"{name} carries no docstring, so it is listed bare"


def test_no_module_is_listed_bare() -> None:
    """Not a style rule — an undocumented module is a hole in what the app
    can truthfully say about itself. Delete this test rather than weaken it
    if that ever stops being worth maintaining."""
    bare = [e["module"] for e in codebase_inventory.subsystems() if not e["summary"]]
    assert bare == []


def test_summaries_are_single_line_and_capped() -> None:
    for entry in codebase_inventory.subsystems():
        assert "\n" not in entry["summary"]
        assert len(entry["summary"]) <= codebase_inventory._MAX_SUMMARY_CHARS + 1


def test_inventory_is_sorted_and_unique() -> None:
    modules = [entry["module"] for entry in codebase_inventory.subsystems()]
    assert modules == sorted(modules)
    assert len(modules) == len(set(modules))


def test_inventory_is_derived_not_hardcoded() -> None:
    """A hand-maintained list would drift back into the wrong answer, so the
    summaries must actually come from the files: this module's own entry
    matches its own docstring."""
    entry = next(
        e
        for e in codebase_inventory.subsystems()
        if e["module"] == "codebase_inventory"
    )
    assert entry["summary"].startswith("Codebase inventory:")
    assert codebase_inventory.__doc__ is not None
    assert entry["summary"].split(".")[0] in " ".join(
        codebase_inventory.__doc__.split()
    )


# --- _summarize -----------------------------------------------------------------


def test_summarize_collapses_whitespace_to_one_line() -> None:
    assert (
        codebase_inventory._summarize(
            "Daily spend caps —\n  a kill-switch\n  for cost."
        )
        == "Daily spend caps — a kill-switch for cost."
    )


def test_summarize_takes_only_the_first_sentence() -> None:
    doc = (
        "Rotating periodic backups of the whole SQLite database file. "
        "Second sentence that should not appear. Third one either."
    )
    summary = codebase_inventory._summarize(doc)
    assert summary == "Rotating periodic backups of the whole SQLite database file."


def test_summarize_extends_past_a_bare_label_first_sentence() -> None:
    """ "Weekly self-report." alone carries no information, so the next
    sentence is pulled in."""
    summary = codebase_inventory._summarize(
        "Weekly self-report. A digest the app writes about itself."
    )
    assert summary == "Weekly self-report. A digest the app writes about itself."


def test_summarize_truncates_at_a_word_boundary_and_marks_it() -> None:
    summary = codebase_inventory._summarize("word " * 200)
    assert len(summary) <= codebase_inventory._MAX_SUMMARY_CHARS + 1
    assert summary.endswith("…")
    assert not summary.endswith(" …")


def test_summarize_empty_docstring() -> None:
    assert codebase_inventory._summarize("   \n  ") == ""


# --- ui_panels ------------------------------------------------------------------


def test_ui_panels_include_the_usage_panel_and_its_sections() -> None:
    """The second failure, pinned: a critique reported this app had no usage
    analytics and proposed building daily-spend and per-model charts the
    Usage panel already draws. The hand-written UI paragraph never mentioned
    it; the frontend's own headings always did."""
    panels = {str(p["panel"]): p["sections"] for p in codebase_inventory.ui_panels()}
    assert "Usage" in panels
    sections = panels["Usage"]
    assert "By model" in sections
    assert "Quality" in sections
    assert "Weekly self-report" in sections
    # the daily-spend chart, whose heading interpolates the window length
    assert any(str(s).startswith("Last ") for s in sections)


def test_ui_panels_cover_the_other_modals() -> None:
    names = {str(p["panel"]) for p in codebase_inventory.ui_panels()}
    for expected in (
        "Bookmarks",
        "Compare models",
        "Document library",
        "Model settings",
        "Templates",
    ):
        assert expected in names


def test_ui_panels_exclude_non_panel_components() -> None:
    """Only an <h2>-titled file is a panel. App.tsx's <h2> is the selected
    conversation's title — a placeholder, not a panel name — and MessageList/
    Sidebar have none at all."""
    components = {str(p["component"]) for p in codebase_inventory.ui_panels()}
    for excluded in ("App", "MessageList", "Sidebar", "Composer"):
        assert excluded not in components


def test_ui_panels_are_sorted_and_have_no_placeholder_headings() -> None:
    panels = codebase_inventory.ui_panels()
    names = [str(p["panel"]) for p in panels]
    assert names == sorted(names)
    for panel in panels:
        assert str(panel["panel"]).strip()
        assert str(panel["panel"]) != "N"
        assert all(str(s) != "N" for s in panel["sections"])  # type: ignore[union-attr]


def test_heading_text_normalizes_jsx_interpolation() -> None:
    assert codebase_inventory._heading_text("Last {data.days} days") == "Last N days"
    assert codebase_inventory._heading_text("  Quality\n  ") == "Quality"
    assert codebase_inventory._heading_text("{anything ? a : b}") == "N"
    assert codebase_inventory._heading_text("<span>Wrapped</span>") == "Wrapped"


def test_ui_panels_parsed_from_markup_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    (tmp_path / "Invented.tsx").write_text(
        "<div><h2>Invented panel</h2><h3>First {n} bit</h3><h3>Second</h3></div>",
        encoding="utf-8",
    )
    (tmp_path / "Invented.test.tsx").write_text(
        "<h2>Test file that must be ignored</h2>", encoding="utf-8"
    )
    (tmp_path / "NoHeading.tsx").write_text("<div>nothing</div>", encoding="utf-8")
    monkeypatch.setattr(codebase_inventory, "_FRONTEND_ROOT", tmp_path)
    assert codebase_inventory.ui_panels() == (
        {
            "component": "Invented",
            "panel": "Invented panel",
            "sections": ["First N bit", "Second"],
        },
    )


def test_ui_panels_degrade_when_frontend_sources_are_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    """A backend-only deployment ships no frontend/src — the note loses the
    panel clause and keeps everything else."""
    monkeypatch.setattr(codebase_inventory, "_FRONTEND_ROOT", tmp_path / "missing")
    assert codebase_inventory.ui_panels() == ()
    assert codebase_inventory.format_ui_lines() == ""


def test_ui_description_names_the_usage_panel() -> None:
    """End of the chain: the `ui` string actually sent to a model."""
    ui = self_describe.capabilities_snapshot(owner=None)["ui"]
    assert "Usage (" in ui
    assert "By model" in ui


def test_ui_panels_ride_on_an_ordinary_capabilities_note() -> None:
    """Ungated, unlike the module inventory — it corrects a claim already
    being sent, and costs ~105 tokens to do it."""
    note = self_describe.format_note(self_describe.capabilities_snapshot(owner=None))
    assert "Panels the user can open" in note
    assert "ALREADY IMPLEMENTED" not in note  # still gated
    assert len(codebase_inventory.format_ui_lines()) < 800


def test_ui_panels_see_the_setup_wizard_with_its_three_steps() -> None:
    """The wizard is a panel with three sections, and the inventory must read
    exactly those — not a comment. The heading reader is a plain regex over
    the source, so a literal tag inside a comment is taken for a heading and
    the "panel name" becomes everything up to the real closing tag: observed
    as a 7,000-character panel entry that blew the size cap. A DOM-level test
    cannot catch that; only the inventory can."""
    panels = {str(p["panel"]): p["sections"] for p in codebase_inventory.ui_panels()}
    assert "First-run setup" in panels
    assert panels["First-run setup"] == [
        "Add your API key",
        "Choose a model preset",
        "Restart and finish",
    ]
    for name in panels:
        assert len(name) < 60, (
            f"a panel name this long is a parsing accident: {name[:80]!r}"
        )


# --- degradation ----------------------------------------------------------------


def test_unreadable_source_tree_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    """An installed-without-sources deployment loses the inventory and keeps
    everything else — never a failed request."""
    monkeypatch.setattr(codebase_inventory, "_PACKAGE_ROOT", tmp_path)
    assert codebase_inventory.subsystems() == ()
    assert codebase_inventory.format_lines() == ""


def test_unparseable_module_is_listed_bare_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    """A file that will not parse still names a subsystem that exists, so it
    is listed without a summary — same reasoning as an undocumented one — and
    never propagates a SyntaxError into a live request."""
    (tmp_path / "broken.py").write_text("def (((", encoding="utf-8")
    (tmp_path / "fine.py").write_text('"""A good module."""\n', encoding="utf-8")
    monkeypatch.setattr(codebase_inventory, "_PACKAGE_ROOT", tmp_path)
    assert codebase_inventory.subsystems() == (
        {"module": "broken", "summary": ""},
        {"module": "fine", "summary": "A good module."},
    )


def test_undocumented_module_is_listed_with_an_empty_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    (tmp_path / "bare.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "fine.py").write_text('"""A good module."""\n', encoding="utf-8")
    monkeypatch.setattr(codebase_inventory, "_PACKAGE_ROOT", tmp_path)
    assert codebase_inventory.subsystems() == (
        {"module": "bare", "summary": ""},
        {"module": "fine", "summary": "A good module."},
    )
    assert "  - `bare`\n" in codebase_inventory.format_lines() + "\n"


def test_non_ascii_docstring_is_read_on_any_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    """This package's docstrings are full of em-dashes and emoji; reading
    them at the platform default encoding (cp1252 on Windows) would raise
    and empty the inventory on exactly the machine this is developed on."""
    (tmp_path / "fancy.py").write_text(
        '"""Quality feedback: a per-answer 👍/👎 rating — nothing else."""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(codebase_inventory, "_PACKAGE_ROOT", tmp_path)
    entries = codebase_inventory.subsystems()
    assert entries[0]["summary"] == (
        "Quality feedback: a per-answer 👍/👎 rating — nothing else."
    )


# --- format_lines ---------------------------------------------------------------


def test_format_lines_frames_the_modules_as_already_built() -> None:
    """Without the framing a bare module list reads as neutral context, and
    the failure being fixed was a model treating implemented subsystems as
    absent."""
    text = codebase_inventory.format_lines()
    assert "ALREADY IMPLEMENTED" in text
    assert "do not propose any of these as new work" in text
    for name in _MISREPORTED_AS_MISSING:
        assert f"`{name}`" in text


# --- looks_like_improvement_request ---------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what are your weaknesses?",
        "What Are Your Limitations",
        "what could you do better",
        "what would you improve about the way you work",
        "how could you be improved?",
        "how would you improve yourself",
        "give me a list of ways to improve this app",
        "write up the app cons and improvements as a spreadsheet",
        "what's wrong with this app",
        # observed live — matched nothing before the self-referential shapes
        "As an app what's your strengths and what improvements do you require",
        "what are your strengths and weaknesses",
        "what improvements do you need",
        "what do you need to improve",
        "tell me the app's limitations",
        "how can you improve yourself",
    ],
)
def test_looks_like_improvement_request_matches(question: str) -> None:
    assert self_describe.looks_like_improvement_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # the overwhelmingly common case in a coding app: a critique request
        # about the USER's work, which must never drag the module inventory in
        "what could be improved in this function?",
        "what are the weaknesses of this argument?",
        "how could I improve my resume",
        "suggest improvements to my SQL query",
        "what's wrong with this regex",
        "list the pros and cons of Postgres vs MySQL",
        "what are the limitations of transformer models",
        "improve this paragraph",
        # ordinary capability questions: answered without the inventory
        "what can you do?",
        "what models do you use",
        "how much budget do I have left",
        # traps for the self-referential phrases: the same nouns about the
        # USER's work, which must never pay for the inventory
        "what are the strengths of this argument",
        "what are the limitations of my approach",
        "what improvements do you suggest for my essay",
        "how do I improve myself",
        # a follow-up with no self-reference does not re-fire: it is grounded
        # through the previous turn's inventory instead (see the re-entry
        # test below), and re-firing would spend ~3,100 tokens on facts that
        # are already in the prompt
        "can you create a plan of how to proceed with the improvements and improve the limitations",
    ],
)
def test_looks_like_improvement_request_no_match(question: str) -> None:
    assert self_describe.looks_like_improvement_request(question) is False


def test_grounded_inventory_survives_re_entry_into_a_later_prompt() -> None:
    """Why widening the phrases is the whole fix for a two-turn critique.

    The follow-up in the observed failure ("make a plan for the improvements
    and improve the limitations") names neither the app nor itself, so no
    phrase can catch it without also catching "improve the limitations of my
    code". It does not need to: strip_per_turn_lines removes only the three
    per-turn marker lines when an assistant answer re-enters a prompt, and
    the inventory block is not one of them — so once the FIRST turn fires,
    the follow-up's model reads the real subsystem list in its context. This
    pins that guarantee; if the inventory ever becomes a per-turn line, the
    follow-up silently loses its grounding again."""
    note = self_describe.format_note(
        self_describe.capabilities_snapshot(owner=None),
        include_subsystems=True,
        answering_model="gpt-5",
        live_tools=["web search"],
    )
    assert "Subsystems ALREADY IMPLEMENTED" in note
    assert "Answering YOU right now" in note
    kept = self_describe.strip_per_turn_lines(note)
    assert "Subsystems ALREADY IMPLEMENTED" in kept
    assert "Answering YOU right now" not in kept


def test_improvement_phrases_anchor_on_the_app() -> None:
    """The invariant that keeps the traps above passing: a phrase must name
    the app or address it in the second person. "cons and improvements" is
    the one documented exception — it has no reading that isn't a request to
    critique something (see _IMPROVEMENT_PHRASES' comment)."""
    anchors = ("you", "your", "yourself", "this app", "the app")
    for phrase in self_describe._IMPROVEMENT_PHRASES:
        if phrase == "cons and improvements":
            continue
        assert any(anchor in phrase for anchor in anchors), phrase


# --- snapshot + note ------------------------------------------------------------


def test_snapshot_always_carries_subsystems() -> None:
    """JSON has no token cost, so GET /v1/capabilities never gates this."""
    snapshot = self_describe.capabilities_snapshot(owner=None)
    modules = {entry["module"] for entry in snapshot["subsystems"]}
    for name in _MISREPORTED_AS_MISSING:
        assert name in modules


def test_capabilities_endpoint_exposes_subsystems(client) -> None:
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    modules = {entry["module"] for entry in response.json()["subsystems"]}
    for name in _MISREPORTED_AS_MISSING:
        assert name in modules


def test_format_note_omits_the_inventory_by_default() -> None:
    note = self_describe.format_note(self_describe.capabilities_snapshot(owner=None))
    assert "ALREADY IMPLEMENTED" not in note
    assert "`db_backup`" not in note


def test_format_note_includes_the_inventory_when_asked() -> None:
    note = self_describe.format_note(
        self_describe.capabilities_snapshot(owner=None), include_subsystems=True
    )
    assert "ALREADY IMPLEMENTED" in note
    for name in _MISREPORTED_AS_MISSING:
        assert f"`{name}`" in note
    # still the ordinary note, not replaced by the listing
    assert "Verified capabilities" in note
    assert "Limits —" in note


def test_including_the_inventory_is_what_costs_tokens() -> None:
    """Pins the reason the default is off — if these ever converge, the gate
    has stopped meaning anything."""
    snapshot = self_describe.capabilities_snapshot(owner=None)
    lean = self_describe.format_note(snapshot)
    full = self_describe.format_note(snapshot, include_subsystems=True)
    # The gate now carries BOTH derived blocks: the module inventory and the
    # main-view controls (each joined with one newline).
    expected = (
        len(codebase_inventory.format_lines())
        + len(codebase_inventory.format_controls_lines())
        + 2
    )
    assert len(full) - len(lean) == expected
    assert len(full) - len(lean) > 5_000


# --- orchestrator wiring --------------------------------------------------------


def test_run_orchestrator_sends_the_inventory_for_a_self_critique_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on the failure this feature exists for: the model that is
    asked to critique the app gets the real module list, so it can no longer
    propose db_backup/ratelimit as new work."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "You should add automated backups and rate limiting."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(
        AskRequest(question="what are your weaknesses?", mode=Mode.smart)
    )

    assert "ALREADY IMPLEMENTED" in result.answer
    for name in _MISREPORTED_AS_MISSING:
        assert f"`{name}`" in result.answer


def test_run_orchestrator_omits_the_inventory_for_an_ordinary_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.smart)
    )

    assert "Verified capabilities" in result.answer
    assert "ALREADY IMPLEMENTED" not in result.answer


def test_litellm_heuristic_fires_for_a_self_critique_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LiteLLM-routed model (Gemini here, but equally Ollama and the budget
    lane) is never offered the app_capabilities tool, so the phrase heuristic
    is its only route to the facts. "what are your weaknesses?" matches none
    of the CAPABILITIES phrases — without the improvement phrases feeding the
    same heuristic, the providers most likely to answer cheaply would answer
    this question with no grounding at all."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_call_model", lambda **_kw: "You should add backups."
    )

    result = run_orchestrator(
        AskRequest(question="what are your weaknesses?", mode=Mode.fast)
    )

    assert "Verified capabilities" in result.answer
    assert "ALREADY IMPLEMENTED" in result.answer
    assert "`db_backup`" in result.answer


def test_litellm_heuristic_still_ignores_a_critique_of_the_users_own_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(
        AskRequest(question="what could be improved in this function?", mode=Mode.fast)
    )
    assert result.answer == "ok"


# --- a free model answers a critique WITH the inventory, not under it --------------
#
# Observed live on ollama/llama3.1:8b: the heuristic path appended the note
# after the answer, so the model wrote its "Improvements" section blind and the
# real module list landed underneath it. The tool path's fix (a second call)
# cannot be used here — in streaming the blind answer is already on screen —
# so the grounded prompt goes into the FIRST call instead, gated on the call
# being free. See orchestrator._self_describe_prompt_grounding.

_LIVE_CRITIQUE = "As an app what's your strengths and what improvements do you require"


def _record_calls(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[dict]:
    seen: list[dict] = []

    def fake_call_model(**kwargs: object) -> str:
        seen.append(dict(kwargs))
        return answer

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    return seen


def test_free_model_critique_gets_the_inventory_in_its_first_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3.1:8b")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _record_calls(monkeypatch, "Strengths: routing. Improvements: none new.")

    result = run_orchestrator(AskRequest(question=_LIVE_CRITIQUE, mode=Mode.fast))

    assert len(seen) == 1  # one call, not a blind one plus a grounded one
    prompt = str(seen[0]["question"])
    assert _LIVE_CRITIQUE in prompt
    assert "ALREADY IMPLEMENTED" in prompt
    assert "`db_backup`" in prompt
    assert "do NOT" in prompt  # the grounded-question instruction, not a dump
    # The answer is the model's grounded text, with the facts still appended
    # so the reader and the follow-up turn see them too.
    assert result.answer.startswith("Strengths: routing. Improvements: none new.")
    assert "ALREADY IMPLEMENTED" in result.answer
    assert "grounded self-describe (facts in prompt)" in result.notes
    assert "second call" not in result.notes


def test_paid_litellm_model_critique_keeps_the_append_after_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is cost, not provider: a paid LiteLLM model still gets the
    note appended rather than ~6,000 prompt tokens it would be billed for."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _record_calls(monkeypatch, "Add backups.")

    result = run_orchestrator(AskRequest(question=_LIVE_CRITIQUE, mode=Mode.fast))

    assert len(seen) == 1
    assert "ALREADY IMPLEMENTED" not in str(seen[0]["question"])
    assert "ALREADY IMPLEMENTED" in result.answer
    assert "grounded self-describe" not in result.notes


def test_a_local_model_the_operator_priced_is_not_free_for_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MODEL_PRICING for a local model means the operator is accounting for
    its compute — the same override that makes estimate_cost stop saying $0."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3.1:8b")
    monkeypatch.setenv("MODEL_PRICING", '{"ollama/llama3.1:8b": [1.0, 2.0]}')
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _record_calls(monkeypatch, "ok")

    run_orchestrator(AskRequest(question=_LIVE_CRITIQUE, mode=Mode.fast))

    assert "ALREADY IMPLEMENTED" not in str(seen[0]["question"])


def test_free_model_ordinary_capability_question_stays_lean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free or not, the inventory is for a critique only."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3.1:8b")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _record_calls(monkeypatch, "gpt-5 and friends")

    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.fast)
    )

    assert "ALREADY IMPLEMENTED" not in str(seen[0]["question"])
    assert "Verified capabilities" in result.answer  # the lean note still lands
    assert "ALREADY IMPLEMENTED" not in result.answer


def test_free_model_critique_reads_the_source_tree_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt's note is reused for the post-answer append."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3.1:8b")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    _record_calls(monkeypatch, "ok")
    real = orchestrator.capabilities_snapshot
    calls: list[int] = []

    def counting(*args: object, **kwargs: object):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "capabilities_snapshot", counting)

    run_orchestrator(AskRequest(question=_LIVE_CRITIQUE, mode=Mode.fast))

    assert len(calls) == 1


def test_stream_free_model_critique_streams_the_grounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason this is a first-call grounding and not a second call: the
    streamed text IS the answer, and there is no taking it back."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3.1:8b")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    prompts: list[str] = []

    def fake_stream_model(**kwargs: object):
        prompts.append(str(kwargs["question"]))
        yield "Grounded critique."

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **_kw: pytest.fail("no second call on the streaming path"),
    )

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question=_LIVE_CRITIQUE, mode=Mode.fast)
        )
    )

    assert len(prompts) == 1
    assert "ALREADY IMPLEMENTED" in prompts[0]
    deltas = [e["data"]["text"] for e in events if e["event"] == "delta"]
    assert deltas[0] == "Grounded critique."
    done = next(e["data"] for e in events if e["event"] == "done")
    assert str(done["answer"]).startswith("Grounded critique.")
    assert "ALREADY IMPLEMENTED" in str(done["answer"])
    assert "grounded self-describe (facts in prompt)" in str(done["notes"])


def test_grounded_answer_gets_the_inventory_in_its_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary shape for a tool-calling turn is the tool call and no
    prose, which re-asks the question with the facts in the prompt (see
    self_describe.grounded_question). That prompt is where the inventory
    actually has to land for the model to write a grounded critique."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    prompts: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        prompts.append(str(kwargs["question"]))
        if len(prompts) == 1:
            kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
            return ""  # the tool call and nothing else
        return "A grounded critique."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(
        AskRequest(question="how could you be improved?", mode=Mode.smart)
    )

    assert len(prompts) == 2
    assert "ALREADY IMPLEMENTED" in prompts[1]
    assert "`db_backup`" in prompts[1]
    assert result.answer == "A grounded critique."


# --- first sentences that must carry a guarantee, not just a name ---------------
#
# The inventory shows a model ONE sentence per module. Each case below is a
# false claim a real critique produced because the operative fact sat in a
# later paragraph the summary never reached — the docstring was correct and
# the summary was uninformative, which is indistinguishable from the feature
# being absent. Rewording the first sentence is the fix; these pin it.


def _summary(module: str) -> str:
    return next(
        e["summary"] for e in codebase_inventory.subsystems() if e["module"] == module
    )


def test_cache_summaries_state_that_a_hit_cannot_outlive_its_inputs() -> None:
    """Claim, made TWICE: the caches can serve stale answers after a document
    or settings change; add versioned invalidation. Both caches have been
    keyed against exactly that since they were written."""
    exact = _summary("cache").lower()
    assert "outlive" in exact
    assert "library" in exact
    semantic = _summary("semantic_cache").lower()
    assert "invalidate" in semantic or "scope key" in semantic


def test_workflow_summary_states_the_step_ceiling() -> None:
    """Claim: workflow mode can over-plan; add hard step ceilings."""
    summary = _summary("workflow").lower()
    assert "cap" in summary
    assert "6" in summary


def test_image_processing_summary_states_the_fine_detail_exemption() -> None:
    """Claim: downscaling can harm diagram/small-text legibility. It is
    skipped for exactly those questions — the difference between a cost
    optimisation and a quality bug."""
    assert "fine detail" in _summary("image_processing").lower()


def test_request_registry_summary_names_its_guarantees() -> None:
    """Previously ran out of room listing the endpoints it covers, without
    ever saying what it guarantees."""
    summary = _summary("request_registry").lower()
    assert "guarantee" in summary
    assert "stop" in summary


def test_self_report_summary_still_names_the_contents() -> None:
    summary = _summary("self_report").lower()
    assert "re-run cost" in summary and "fallback causes" in summary


def test_a_truncated_summary_still_ends_on_its_point_not_mid_clause() -> None:
    """Truncation is acceptable; losing the operative fact is not. Every
    module whose summary is cut must have said what it guarantees first —
    checked here for the ones that earned a rewrite, since a general rule
    cannot be asserted mechanically."""
    for module in ("workflow", "request_registry"):
        summary = _summary(module)
        assert summary.endswith("…")
        # the guarantee survives the cut
        assert len(summary.split("…")[0]) > 100


# --- ui_controls: the third surface ---------------------------------------------
#
# Third occurrence of the one failure genus: a critique proposed
# "per-conversation model pins" and "pre-flight cost estimates in the UI" —
# both shipped, both in main-view chrome invisible to the module inventory
# (backend) and the panel inventory (<h2> files). Controls describe
# themselves through the labels accessibility already requires.


def test_ui_controls_include_the_two_features_a_critique_reproposed() -> None:
    all_labels = {
        label
        for entry in codebase_inventory.ui_controls()
        for label in entry["labels"]  # type: ignore[union-attr]
    }
    assert "Pinned model" in all_labels
    assert any(
        label.startswith("Worst-case estimate before sending") for label in all_labels
    )


def test_ui_controls_are_the_complement_of_panels() -> None:
    """Panels describe themselves via headings; controls cover the rest —
    including App.tsx, whose <h2> is the conversation title (which is why
    ui_panels drops it) and which hosts the model pin."""
    control_components = {str(e["component"]) for e in codebase_inventory.ui_controls()}
    panel_components = {str(p["component"]) for p in codebase_inventory.ui_panels()}
    assert "App" in control_components
    assert not (control_components & panel_components)


def test_ui_controls_skip_dynamic_labels_and_keep_static_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clear_inventory_cache
) -> None:
    (tmp_path / "Widget.tsx").write_text(
        '<button aria-label="Static control" title={`Bookmark ${id}`} />'
        '<select title="Choose a thing" aria-label={dyn} />',
        encoding="utf-8",
    )
    (tmp_path / "APanel.tsx").write_text(
        '<div><h2>A panel</h2><button aria-label="Panel-internal control"/></div>',
        encoding="utf-8",
    )
    monkeypatch.setattr(codebase_inventory, "_FRONTEND_ROOT", tmp_path)
    controls = codebase_inventory.ui_controls()
    assert controls == (
        {"component": "Widget", "labels": ["Choose a thing", "Static control"]},
    )


def test_ui_controls_labels_are_deduped_sorted_and_capped() -> None:
    for entry in codebase_inventory.ui_controls():
        labels = entry["labels"]
        assert labels == sorted(set(labels))  # type: ignore[arg-type]
        assert all(
            len(str(label)) <= codebase_inventory._MAX_LABEL_CHARS for label in labels
        )  # type: ignore[union-attr]


def test_controls_ride_the_critique_gate_not_every_note(db_path) -> None:
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert any(e["component"] == "App" for e in snapshot["ui_controls"])
    plain = self_describe.format_note(snapshot)
    critique = self_describe.format_note(snapshot, include_subsystems=True)
    assert "In-view controls" not in plain
    assert "In-view controls" in critique
    assert "Pinned model" in critique
