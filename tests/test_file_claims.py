"""The unfulfilled file-claim note (app/file_claims.py) and its orchestrator
wiring.

The live failure this pins: asked to critique the app, the model — with
code execution available — wrote CSV-producing Python as TEXT, then closed
with "I created a CSV listing key weaknesses..." (message 368, gpt-5,
code_results empty). No code ran, no file existed, and the claim stood
uncorrected. The question-side artefact guard never saw it, because the
user never asked for a file.
"""

from __future__ import annotations

import pytest

import app.orchestrator as orchestrator
from app import file_claims
from app.orchestrator import run_orchestrator
from app.schemas import AskRequest, Mode

# A close paraphrase of the observed answer: code as text + a completed
# first-person claim, no execution anywhere.
OBSERVED_ANSWER = (
    "import csv\nfrom datetime import datetime\n\n"
    'filename = "ai-orchestrator_weaknesses_and_mitigations.csv"\n'
    'headers = ["Area", "Weakness", "Impact"]\n'
    'rows = [["Data storage", "Single local SQLite database", "..."]]\n'
    'with open(filename, "w", newline="", encoding="utf-8") as f:\n'
    "    writer = csv.writer(f)\n"
    "    writer.writerow(headers)\n\n"
    "I created a CSV listing key weaknesses with practical offsets and "
    "concrete improvement ideas."
)


# --- detection: should fire -------------------------------------------------------


def test_fires_on_the_observed_answer() -> None:
    assert file_claims.claims_unproduced_file(OBSERVED_ANSWER, []) is True


def test_fires_on_a_passive_claim() -> None:
    text = 'df.to_csv("out.csv")\n\nThe CSV file has been saved with all rows.'
    assert file_claims.claims_unproduced_file(text, []) is True


def test_fires_on_a_contracted_claim_with_extension() -> None:
    text = 'with open("report.xlsx", "wb") as f:\n    f.write(data)\n\nI\'ve saved everything to report.xlsx for you.'
    assert file_claims.claims_unproduced_file(text, []) is True


# --- detection: must NOT fire -----------------------------------------------------


def test_silent_when_code_actually_ran() -> None:
    """The claim is simply true once code_results exist."""
    results = [{"code": "x", "output": "", "files": []}]
    assert file_claims.claims_unproduced_file(OBSERVED_ANSWER, results) is False


def test_silent_on_delivered_code_described_in_third_person() -> None:
    """The user asked for a script; the answer IS the deliverable. 'This
    script creates a CSV' is a description of the code, not a claim that a
    file exists."""
    text = (
        "Here's the script you asked for:\n\n"
        'with open("out.csv", "w") as f:\n    csv.writer(f).writerow(row)\n\n'
        "This script creates a CSV with one row per record. Run it with "
        "python export.py."
    )
    assert file_claims.claims_unproduced_file(text, []) is False


def test_silent_on_a_claim_with_no_writing_code() -> None:
    """'I created a comparison table' with the table rendered inline is an
    ordinary, true answer."""
    text = "I created a comparison table below.\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert file_claims.claims_unproduced_file(text, []) is False


def test_silent_on_code_that_only_reads() -> None:
    text = (
        'with open("data.csv") as f:\n    rows = list(csv.reader(f))\n\n'
        "I created a summary of your csv below."
    )
    assert file_claims.claims_unproduced_file(text, []) is False


def test_claim_cannot_splice_across_sentences() -> None:
    """'I created a plan. ... file' must not stitch into a match."""
    text = (
        'open("x.csv", "w")\n\n'
        "I created a plan for you. Later you could add a config file if needed."
    )
    assert file_claims.claims_unproduced_file(text, []) is False


def test_silent_on_empty_answer() -> None:
    assert file_claims.claims_unproduced_file("", []) is False


# --- the note ---------------------------------------------------------------------


def test_note_offers_a_next_step_when_the_tool_was_available() -> None:
    note = file_claims.format_note(True)
    assert "never executed" in note
    assert "run it" in note


def test_note_explains_when_the_tool_was_not_available() -> None:
    note = file_claims.format_note(False)
    assert "never executed" in note
    assert "CODE_EXECUTION" in note


# --- orchestrator wiring ----------------------------------------------------------


def _fake(monkeypatch: pytest.MonkeyPatch, answer: str, with_result: bool = False):
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        if with_result:
            kwargs["code_results"].append(  # type: ignore[union-attr]
                {"code": "print(1)", "output": "1", "files": []}
            )
        return answer

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)


def test_run_orchestrator_appends_the_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on the observed failure: the false claim no longer gets
    the last word."""
    _fake(monkeypatch, OBSERVED_ANSWER)
    result = run_orchestrator(
        AskRequest(question="what are your weaknesses?", mode=Mode.smart)
    )
    assert "no file was actually produced" in result.answer


def test_run_orchestrator_leaves_a_backed_claim_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch, OBSERVED_ANSWER, with_result=True)
    result = run_orchestrator(
        AskRequest(question="what are your weaknesses?", mode=Mode.smart)
    )
    assert "no file was actually produced" not in result.answer


def test_run_orchestrator_leaves_an_ordinary_answer_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch, "The capital of Australia is Canberra.")
    result = run_orchestrator(
        AskRequest(question="capital of Australia?", mode=Mode.fast)
    )
    assert "no file was actually produced" not in result.answer
