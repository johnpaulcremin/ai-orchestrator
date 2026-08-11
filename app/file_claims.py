"""Detects an answer that CLAIMS to have created a file when no code ran,
and supplies the honest note appended in its place — a claim about a file
that does not exist must not be the answer's last word.

Observed live before this existed: asked to critique the app, gpt-5 — with
the code_interpreter tool attached and CODE_EXECUTION on — wrote the
CSV-producing Python out as TEXT, then closed with "I created a CSV listing
key weaknesses...". No code ran, no file existed, and nothing corrected the
claim. The question-side guard (orchestrator._looks_like_artefact_request,
which forces code execution for a plain "make me a spreadsheet" ask) cannot
see this case: the user never asked for a file — the model volunteered one,
then narrated an execution that never happened. Same genus as the
confabulated tool call CAPABILITIES_IDENTITY_LINE exists to prevent ("never
write a tool call out as text"), one tool over.

Detection is deliberately double-keyed; BOTH signals are required:

1. A first-person, COMPLETED creation claim naming a file-ish artifact
   ("I created a CSV ...", "I've saved the results to report.xlsx"), or its
   passive twin ("the file has been created"). Not "this script creates a
   CSV": an answer legitimately DELIVERING code the user asked for
   describes what that code would do in exactly those third-person,
   present-tense terms, and must not be flagged for it.
2. File-writing code in the answer body (open(..., "w"), csv.writer,
   .to_csv, ...). A claim with no code is far more likely prose about
   something rendered inline ("I created a comparison table below").

Only consulted when code_results is empty — when code actually ran and
files came back, the claim is simply true. Same phrase-list discipline as
fact_check/self_describe: err toward missing a lie over branding a
legitimate answer with a warning it did not earn.
"""

from __future__ import annotations

import re

# A file-ish noun or an explicit extension, required within the claim's own
# clause (no crossing a sentence boundary — the [^.\n] windows below) so
# "I created a plan. Check the config file." cannot splice into a match.
_FILE_WORD = (
    r"(?:\.csv|\.xlsx|\.xls|\.json|\.txt|\.md|\.pdf|\.zip|\.docx|\.pptx|"
    r"csv|xlsx|excel|spreadsheet|workbook|pdf|zip|docx|pptx|file\b)"
)

# First-person, completed: "I created/generated/saved/exported ... <file>".
# Deliberately NOT "creates"/"will create"/"this script creates" — those are
# descriptions of delivered code, not claims of a finished artifact.
_CLAIM_RE = re.compile(
    r"\bI(?:'ve|\s+have)?\s+"
    r"(?:created|generated|saved|produced|exported|written|wrote|built)\b"
    r"[^.\n]{0,80}?" + _FILE_WORD,
    re.IGNORECASE,
)

# Passive twin: "the/a/your ... file/CSV/... has been/was created/saved...".
_PASSIVE_CLAIM_RE = re.compile(
    r"\b(?:the|a|your)\s+[^.\n]{0,40}?"
    r"(?:file|csv|xlsx|spreadsheet|workbook|pdf|zip|docx|pptx)\s+"
    r"(?:has\s+been|have\s+been|was|were|is\s+now)\s+"
    r"(?:created|generated|saved|written|produced|exported)\b",
    re.IGNORECASE,
)

# File-writing code shapes. Anchored to things that WRITE, not merely read:
# open() only with a w/a/x mode string, plus the usual library writers.
_CODE_WRITE_RE = re.compile(
    r"open\s*\([^)\n]{0,120}[\"'](?:w|wb|a|ab|x|xb)[\"']"
    r"|csv\.writer\s*\("
    r"|\.writerow(?:s)?\s*\("
    r"|\.to_csv\s*\("
    r"|\.to_excel\s*\("
    r"|\.savefig\s*\("
    r"|json\.dump\s*\("
    r"|\.write_text\s*\("
    r"|ZipFile\s*\([^)\n]{0,120}[\"'][wax][\"']"
    r"|Workbook\s*\(",
    re.IGNORECASE,
)


def claims_unproduced_file(answer_text: str, code_results: list[object]) -> bool:
    """True when the answer asserts a file exists that nothing produced —
    a completed-creation claim plus file-writing code, with no code result
    to back either. See the module docstring for why both keys are
    required."""
    if code_results:
        return False
    text = answer_text or ""
    if not (_CLAIM_RE.search(text) or _PASSIVE_CLAIM_RE.search(text)):
        return False
    return _CODE_WRITE_RE.search(text) is not None


def format_note(code_execution_offered: bool) -> str:
    """The correction appended under the false claim. Names the next step
    the reader can actually take, which differs by whether the tool was
    even available to the model that answered."""
    base = (
        "Note: no file was actually produced — the code above was written "
        "out but never executed, so the file it describes does not exist."
    )
    if code_execution_offered:
        return f"{base} Ask me to run it, or regenerate this answer."
    return (
        f"{base} Code execution was not available for this answer (the "
        "CODE_EXECUTION feature is off, or the answering model does not "
        "support it)."
    )
