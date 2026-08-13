"""Detects an answer that CLAIMS an image exists when none was generated,
and supplies the honest note appended in its place — the image twin of
app/file_claims.py, written for the same reason and to the same rules.

Observed live, twice, and the second one is the reason this module exists.
First a Claude smart-tier turn narrated an intended action as a real one:
"Generating: a router diagram with a central hub, arrows to three generic
tech-style icons...". Then, asked "where's the image?", an Ollama budget-tier
turn went further and described a picture that had never existed in any form:
"The generated image is being displayed inline with this response. It shows a
router diagram with a central hub and arrows pointing to three generic
tech-style icons labeled OpenAI, Anthropic, and Google... This image has been
generated using OpenAI's gpt-image-1 tool, which was triggered by your
explicit 'generate an image' request." Every clause of that was invented,
down to the tool name.

Why the existing guards cannot catch it. The question-side heuristic
(orchestrator_tools._looks_like_image_request) reads the QUESTION, and
"where's the image?" is not a request for one — correctly, it did not fire.
The per-turn grounding (self_describe.format_note) tells the model which
tools it actually holds, but only on a turn where self-description fires at
all, and a casual "where's the image?" routes to casual_chat with no
grounding attached. This runs on the ANSWER, unconditionally, and is the only
guard positioned to see a claim about an image on a turn that never involved
one.

Detection needs a second key for the same reason file_claims does, but the
discriminator here is TENSE rather than a code shape, because what makes an
image claim false is that it is about THIS response:

1. The claim must be completed or in progress, never hypothetical:
   first-person ("I've generated an image"), passive ("the image has been
   generated"), presentational ("the image is displayed below"), or narrated
   ("Generating: a router diagram..."). Deliberately NOT "I can generate an
   image" or "a diagram of this would show...": an answer explaining what it
   COULD do, or describing what a picture would contain, claims nothing.
2. Present and present-perfect stand alone — "has been generated", "is being
   displayed" place the act at this turn. SIMPLE PAST does not: "the diagram
   was created in 1974" is history, not a claim about this answer, so the
   past form must ALSO be presentational ("was shown below") to count.

A turn discussing an image the USER attached ("the image you sent shows a
flowchart") matches none of these, and is left alone.

Only consulted when no images came back for the turn — when generation
actually happened, the claim is simply true. Same discipline as
fact_check/self_describe/file_claims: err toward missing a lie over branding
a legitimate answer with a warning it did not earn.
"""

from __future__ import annotations

import re

# The picture nouns a claim can be about. Narrower than
# orchestrator_tools._PICTURE_NOUNS: that list decides whether to SPEND money
# on a request, this one decides whether to contradict an answer, and the
# vaguer members of that list ("visual", "graphic", "render") are ordinary
# words in prose about design or code.
_IMAGE_WORD = (
    r"(?:image|picture|photo|diagram|illustration|drawing|artwork|"
    r"mockup|infographic|flowchart)"
)

# "displayed inline with this response", "shown below", "attached above".
_PRESENTED = (
    r"(?:displayed|shown|rendered|attached|included|embedded|"
    r"above|below|inline|here)"
)

# First-person, COMPLETED: "I generated an image", "I've created the diagram".
# Deliberately NOT "I can generate"/"I would create"/"I'll draw" — an answer
# describing a capability or an intention is not claiming a finished artifact.
_CLAIM_RE = re.compile(
    r"\bI(?:'ve|\s+have)?\s+"
    r"(?:generated|created|produced|made|drawn|rendered|attached)\b"
    r"[^.\n]{0,60}?" + _IMAGE_WORD,
    re.IGNORECASE,
)

# Passive twin, present perfect or present: "the image has been generated",
# "the generated image is being displayed". These tenses put the act at THIS
# turn, so they stand on their own.
_PASSIVE_RECENT_RE = re.compile(
    r"\b(?:the|this|your|a)\s+(?:\w+\s+){0,2}?" + _IMAGE_WORD + r"\s+"
    r"(?:has\s+been|have\s+been|is\s+now|is\s+being|are\s+being|is|are)\s+"
    r"(?:being\s+)?"
    r"(?:generated|created|produced|rendered|" + _PRESENTED + r")\b",
    re.IGNORECASE,
)

# Simple past is the tense that carries history as easily as it carries this
# turn — "the diagram was created in 1974" is not a claim about this answer.
# So the past form has to ALSO be presentational to count: "was displayed
# below" is about this response in a way "was created" is not.
_PASSIVE_PAST_RE = re.compile(
    r"\b(?:the|this|your|a)\s+(?:\w+\s+){0,2}?" + _IMAGE_WORD + r"\s+"
    r"(?:was|were)\s+(?:being\s+)?(?:" + _PRESENTED + r")\b",
    re.IGNORECASE,
)

# In-progress narration presented as fact: "Generating: a router diagram...".
# A model that announces the act mid-answer has, in this codebase, no way to
# perform it — the image call is made by the orchestrator around the answer,
# never by the model mid-sentence.
_NARRATION_RE = re.compile(
    r"^\s*(?:generating|creating|drawing|rendering)\s*:\s*\S"
    r"|\bI(?:'m|\s+am)\s+(?:now\s+)?(?:generating|creating|drawing|rendering)\b"
    r"[^.\n]{0,60}?" + _IMAGE_WORD,
    re.IGNORECASE | re.MULTILINE,
)


def claims_unproduced_image(answer_text: str, generated_images: list[str]) -> bool:
    """True when the answer asserts an image exists that nothing generated.

    See the module docstring for why the tense split is the second key, and
    why the question-side heuristic and the per-turn grounding both
    structurally miss this case.
    """
    if generated_images:
        return False
    text = answer_text or ""
    return any(
        pattern.search(text)
        for pattern in (
            _NARRATION_RE,
            _CLAIM_RE,
            _PASSIVE_RECENT_RE,
            _PASSIVE_PAST_RE,
        )
    )


def format_note(image_generation_available: bool) -> str:
    """The correction appended under the false claim. Names the next step
    the reader can actually take, which differs by whether the feature was
    reachable on this turn at all — the same split file_claims makes on
    code execution."""
    base = (
        "Note: no image was generated for this answer. The description above "
        "is invented — there is no picture attached to this message."
    )
    if image_generation_available:
        return (
            f"{base} Image generation IS available here; it just wasn't "
            'triggered by this question. Ask for one directly ("draw me a '
            'diagram of X") and it will fire.'
        )
    return (
        f"{base} Image generation was not available for this answer — the "
        "IMAGE_GENERATION feature is off (Settings > Image generation)."
    )
