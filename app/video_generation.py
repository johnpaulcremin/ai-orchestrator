"""Optional video generation: a standalone, heuristic-triggered call to a
hosted text-to-video model (OpenAI's Sora, Google's Veo, or Runway), returning
ready-to-render `data:video/mp4;base64,...` URLs.

Same "standalone call gated by a phrase heuristic" design as the fact-check and
Gemini/Imagen image paths, and for the same reason: no provider offers a hosted
video tool a chat model can call mid-answer, and this app has no client-side
tool-execution loop to hand a model a tool the provider doesn't host itself. So
something other than the model decides when a video is wanted, and the feature
works on every tier rather than only where a particular vendor answered.

Two things make video different from every other optional call here, and both
shape what follows:

ASYNCHRONOUS. Image generation returns the image. Video generation returns a
JOB — an id and a status — and the bytes only exist once the provider finishes
rendering, tens of seconds to minutes later. So this module submits, polls, and
only then downloads, all inside the one request the user is waiting on. That
blocking wait is why VIDEO_GENERATION_TIMEOUT exists and why it is finite: a
request that hangs until the provider gives up is worse than one that returns
prose and says the video didn't arrive in time.

EXPENSIVE. A clip costs 10-100x an image — dollars, not cents, for a few
seconds. Every design choice here is therefore biased harder toward NOT firing
than the image heuristic is: one false positive is a meaningful amount of real
money. The daily-spend cap is load-bearing for this feature in a way it is not
for a $0.02 image — and it is the ONLY guard that is: the composer's live cost
preview (/v1/estimate) prices tokens only, so it quotes nothing for the clip.
Worth knowing before enabling this on a deployment without a cap set.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any

from .settings import bool_setting
from .telemetry import logger

# A generated clip is base64'd into the answer JSON and stored inline in
# SQLite, exactly like a generated image or a code-execution file. The same
# ~10MB ceiling those use applies for the same reason: past it, the cost is
# paid by every later read of the conversation, not just the one that made it.
_MAX_VIDEO_BYTES = 10 * 1024 * 1024

# How often to ask the provider whether the job is done. Two seconds is short
# enough that a fast render isn't left sitting finished, and long enough that a
# three-minute one costs ~90 status calls rather than thousands.
_POLL_INTERVAL_SECONDS = 2.0

# Terminal job states. Anything else means "still rendering, keep polling".
_DONE_STATUSES = frozenset({"completed", "succeeded", "success"})
_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled"})


def video_generation_enabled() -> bool:
    """Opt-in: VIDEO_GENERATION=true (env, or a saved Settings override — same
    override > env > default chain as any other feature flag). Off by default,
    like every other flag here that spends money."""
    return bool_setting("VIDEO_GENERATION", False)


def video_generation_model() -> str:
    """Which text-to-video model to call. The default is OpenAI's Sora, chosen
    because it needs no key this app doesn't already require — OPENAI_API_KEY is
    mandatory anyway (the auto router's classifier runs on it), so switching the
    flag on is the only setup step. `gemini/veo-...` (GEMINI_API_KEY) and
    `runwayml/...` (RUNWAYML_API_SECRET) are the alternatives, selected by the
    same prefix convention every other model setting in this app uses."""
    return (os.getenv("VIDEO_GENERATION_MODEL") or "").strip() or "sora-2"


def video_generation_seconds() -> str:
    """Clip length, as the string the provider's API expects. Short by default:
    this is billed per second of output, so the default is the one that costs
    least while still being a video."""
    return (os.getenv("VIDEO_GENERATION_SECONDS") or "").strip() or "4"


def video_generation_size() -> str:
    return (os.getenv("VIDEO_GENERATION_SIZE") or "").strip() or "720x1280"


def _timeout_seconds() -> float:
    """How long to wait for a submitted job before giving up on it.

    Finite on purpose, and not because the provider needs the hint: this poll
    blocks the HTTP request the user is waiting on, so the ceiling is really a
    promise about the worst case they can be made to sit through. Three minutes
    covers a normal short-clip render with room to spare; past that, prose plus
    an honest "it didn't arrive in time" beats a request that never returns.
    """
    raw = (os.getenv("VIDEO_GENERATION_TIMEOUT") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 180.0
    return value if value > 0 else 180.0


# --- the trigger --------------------------------------------------------------
#
# Same two-grammar shape as orchestrator_tools._looks_like_image_request (a verb
# that asks for a moving picture on its own, plus maker-verb + video-noun), and
# deliberately tighter, because a false positive here costs dollars rather than
# cents.
#
# The hard part is that "video" is overwhelmingly a MODIFIER in ordinary
# English — video game, video call, video card, video conferencing — so the noun
# rule cannot simply look for the word. It has to check what follows it.

# Verbs that ask for a moving picture on their own: whatever follows is the
# subject ("animate this logo"). "animate" is the only one that carries the
# request without a noun, and even it has an abstract sense guarded below.
_MOTION_VERBS = ("animate", "re-animate", "reanimate")

# Maker verbs: these mean "make a video" only when a video-noun follows.
#
# Deliberately SHORTER than the image heuristic's list — "show" and "give" are
# missing, and their absence is the point. For a picture, "show me a diagram of
# X" can only mean "produce one". For a moving picture it far more often means
# "find me one that exists": "show me the trailer for Dune", "show me a clip
# from that movie", "give me a movie about time travel to watch tonight" all
# matched, and all would have rendered a clip nobody asked for at a dollar or
# two each. Losing "show me a video of a cat" to keep those three is the right
# side of a trade this feature's price makes for us.
_MAKER_VERBS = (
    "generate",
    "create",
    "make",
    "produce",
    "render",
    "shoot",
    "film",
    "whip up",
)

# A question ABOUT making a video is not a request to make one. These openers
# are how-to, cost and debugging questions — "How do I make a timelapse with
# ffmpeg?", "What does it cost to produce a film in the UK?", "Why does my code
# render a video that is out of sync?" — every one of which matched the verb
# rule cleanly, because grammatically they do contain "make a timelapse".
_QUESTION_OPENERS = (
    "how do i",
    "how do you",
    "how can i",
    "how would i",
    "how to",
    "what does it cost",
    "what would it cost",
    "how much does it cost",
    "how much would it cost",
    "why does",
    "why did",
    "why is",
    "what is the best way to",
    "what's the best way to",
    "is there a way to",
    "can i use",
    "should i use",
)

# Tooling named anywhere in the question means it is about PRODUCING video with
# software, not about this app rendering one: "make an animation in CSS",
# "create a GIF from a video file in Python", "make a timelapse with ffmpeg",
# "render a movie in Blender from my scene file". A veto rather than part of the
# match, so it can only ever reduce firing.
_TOOLING_MARKERS = (
    "css",
    "ffmpeg",
    "blender",
    "imagemagick",
    "framer motion",
    "after effects",
    "premiere",
    "davinci",
    "final cut",
    "python",
    "javascript",
    "typescript",
    "react",
    "svg",
    "canvas",
    "webgl",
    "unity",
    "unreal",
    "manim",
    "moviepy",
)

_VIDEO_NOUNS = (
    "video",
    "videos",
    "animation",
    "animations",
    "clip",
    "clips",
    "movie",
    "movies",
    "film",
    "films",
    "gif",
    "gifs",
    "timelapse",
    "time-lapse",
    "montage",
    "trailer",
    "reel",
    "cutscene",
    "b-roll",
)

# A video noun only counts when it is the HEAD of the phrase — the thing being
# asked for — rather than a modifier on some other noun. This is an ALLOWLIST of
# what may follow it, and that direction is the whole point.
#
# It began as a denylist of disqualifying next-words (game, call, card, codec,
# tutorial, ...). That shape cannot win: "video" is overwhelmingly a modifier in
# ordinary English, so the denylist has to enumerate every noun anyone might put
# behind it, and each one it misses is a paid false positive. Testing it found
# three in a minute — "how do I make a video load faster" (load), "produce a
# report on video engagement" (engagement), "make a video-editing checklist"
# (hyphenated, so nothing followed the word at all). There would have been more.
#
# Inverting it makes the failure mode safe: an unanticipated next-word now means
# "not a video request" (free, wrong once) instead of "generate a clip" (billed,
# wrong every time). The same trick orchestrator_tools._AMBIGUOUS_HEAD_NOUN
# already uses for "visual"/"graphic", for the same reason.
_HEAD_CONNECTIVES = frozenset(
    {
        "of",
        "about",
        "for",
        "with",
        "showing",
        "depicting",
        "featuring",
        "illustrating",
        "demonstrating",
        "explaining",
        "that",
        "which",
        "where",
        "in",
        "from",
        "please",
        "now",
    }
)

# Prepositions/connectives that mean the maker verb has reached ACROSS one noun
# phrase into another: in "produce a report on video engagement" the verb's real
# object is the report, and "video" belongs to a different phrase entirely. The
# adjective slack that lets "make a short looping video" match is what allows
# that reach, so the span it matched is checked for these.
_PHRASE_BOUNDARIES = frozenset(
    {"on", "about", "of", "for", "in", "with", "from", "regarding", "covering"}
)


# "animate" has an abstract sense — an animated DISCUSSION, animating a
# DEBATE — and a technical one that is not video at all: a CSS animation, an
# animated component. Both would otherwise buy a clip.
_ABSTRACT_ANIMATE_OBJECTS = frozenset(
    {
        "discussion",
        "discussions",
        "debate",
        "debates",
        "conversation",
        "conversations",
        "crowd",
        "audience",
        "css",
        "component",
        "components",
        "element",
        "elements",
        "div",
        "svg",
        "button",
        "buttons",
        "transition",
        "transitions",
        "sprite",
        "sprites",
        "chart",
        "charts",
        "graph",
        "graphs",
        "spinner",
        "loader",
        "modal",
        "sidebar",
        "icon",
        "icons",
        "cursor",
        "logo's",
        # Programming objects. "generate a clip of code that reverses a string"
        # is a request for a code snippet that happens to use the word "clip";
        # the same veto now covers both loops, so it reads correctly here too.
        "code",
        "snippet",
        "function",
        "script",
        "config",
        "json",
        "yaml",
        "query",
        "regex",
    }
)

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
        "between",
        "against",
        "with",
        "without",
        "before",
        "after",
        "toward",
        "towards",
        "about",
    }
)


def _alternation(words: tuple[str, ...]) -> str:
    """Longest-first so "time-lapse" cannot be half-matched as "time"."""
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_RECIPIENT = r"(?:(?:me|us|for me|for us)\s+)?"
_ARTICLE = r"(?:(?:a|an|the|some|this|that|another|one|my|your|its)\s+)?"
# Up to three adjectives between the article and the noun, matching the image
# heuristic's allowance: "make a short looping video" lands.
_MODIFIERS = r"(?:[\w'-]+\s+){0,3}?"

_WORD_RE = re.compile(r"[\w'-]+")

# The video noun, the span of adjectives the match had to cross to reach it,
# and whatever trails it — the last two are what decide whether the noun is the
# HEAD of the phrase or a modifier on something else. Captured rather than
# excluded with a lookahead for the same reason the image heuristic captures its
# head: every group before it is optional, so a lookahead would backtrack into
# the article and never fire.
_VIDEO_NOUN_RE = re.compile(
    rf"\b(?:{_alternation(_MAKER_VERBS)})\s+"
    rf"{_RECIPIENT}{_ARTICLE}(?P<mods>{_MODIFIERS})"
    rf"(?:{_alternation(_VIDEO_NOUNS)})\b"
    r"(?P<tail>.{0,24})"
)


def _is_head_noun(tail: str) -> bool:
    """Whether the video noun just matched is the thing being asked for.

    `tail` is the raw text immediately following it, so the FIRST CHARACTER
    carries most of the signal and is checked before any word splitting:

    - nothing left, or sentence punctuation -> the noun ended the phrase, so it
      is the head ("make me a video.").
    - a hyphen/slash/underscore -> a compound, and the noun is the modifier half
      of it. This is the case a word-based check cannot see at all: in "make a
      video-editing checklist" no whitespace follows "video", so splitting on
      words finds "checklist" two tokens away, or nothing.
    - any other non-space -> glued to something else; not a head.
    - whitespace -> the next word decides, and it must be a connective that
      keeps the noun the head ("a video OF a cat", "a video SHOWING x").
    """
    if not tail.strip():
        return True
    if tail[0] in "-_/\\":
        return False
    if tail[0] in ".,!?;:)]—":
        return True
    if not tail[0].isspace():
        return False
    words = _WORD_RE.findall(tail)
    if not words:
        return False
    # A video noun may be followed by ANOTHER video noun and still be the head:
    # "video clip", "video montage", "movie trailer", "animation clip" are
    # compound names for one artefact. Without this the most canonical phrasing
    # of all — "make a video clip of a dog" — matched on "video", saw "clip"
    # behind it, and rejected the whole request; the regex consumes its tail, so
    # `finditer` never got to try "clip" as a head of its own.
    return words[0] in _HEAD_CONNECTIVES or words[0] in _VIDEO_NOUNS


_MOTION_VERB_RE = re.compile(
    rf"\b(?:{_alternation(_MOTION_VERBS)})\s+"
    rf"{_RECIPIENT}{_ARTICLE}"
    r"(?P<rest>.*)"
)

# How far past the verb to look for the thing being animated. The head word
# alone is not enough: "animate the loading spinner" puts an innocent adjective
# ("loading") in the head slot and the disqualifying noun ("spinner") behind it,
# so a head-only check bought a clip for a CSS spinner. Bounded rather than
# unbounded so a later, unrelated clause ("animate this logo, then chart the
# spend") cannot veto a genuine request.
_ANIMATE_LOOKAHEAD_WORDS = 4


def looks_like_video_request(question: str) -> bool:
    """Whether this question asks for a video to be GENERATED.

    Errs toward missing a request over over-triggering, harder than any other
    heuristic in this app: a false positive here spends dollars, not cents.
    """
    text = " ".join((question or "").lower().split())

    # Two whole-question vetoes, checked first because they are cheap and
    # because neither can be expressed inside the match: a question ABOUT making
    # a video is not a request to make one, and a question naming video tooling
    # is about producing one with software, not about this app rendering it.
    if text.startswith(_QUESTION_OPENERS):
        return False
    if any(marker in text for marker in _TOOLING_MARKERS):
        return False

    for match in _VIDEO_NOUN_RE.finditer(text):
        # The adjective slack that lets "make a short looping video" match also
        # lets the verb reach across a whole noun phrase into another one, which
        # is how "produce a report on video engagement" matched: the verb's real
        # object is the report, and "video" belongs to a later phrase entirely.
        crossed = _WORD_RE.findall(match.group("mods"))
        if any(word in _PHRASE_BOUNDARIES for word in crossed):
            continue
        if not _is_head_noun(match.group("tail")):
            continue
        # The same objects that disqualify "animate X" disqualify "make an
        # animation OF X". This list used to be consulted only in the motion-verb
        # loop below, which is why "animate the css transition" was correctly
        # rejected while "create a CSS animation for a spinner" was not.
        trailing = _WORD_RE.findall(match.group("tail"))[:_ANIMATE_LOOKAHEAD_WORDS]
        if any(word in _ABSTRACT_ANIMATE_OBJECTS for word in trailing):
            continue
        return True

    for match in _MOTION_VERB_RE.finditer(text):
        following = _WORD_RE.findall(match.group("rest"))[:_ANIMATE_LOOKAHEAD_WORDS]
        if not following:
            continue
        if following[0] in _NON_SUBJECT_HEADS:
            continue
        if any(word in _ABSTRACT_ANIMATE_OBJECTS for word in following):
            continue
        return True

    return False


# --- the call -----------------------------------------------------------------


def _litellm() -> Any:
    """Imported lazily, matching providers._litellm: LiteLLM is slow to import
    and a deployment that never generates a video should never pay for it."""
    import litellm

    litellm.drop_params = True
    return litellm


def _status_of(job: Any) -> str:
    return str(getattr(job, "status", "") or "").strip().lower()


def _job_id_of(job: Any) -> str:
    return str(getattr(job, "id", "") or "").strip()


def generate_video(prompt: str) -> list[str]:
    """Generate one clip for `prompt`, as a ready-to-render
    `data:video/mp4;base64,...` URL in a single-element list (or [] on any
    failure).

    Returns a list rather than one optional string purely so the field it
    populates has the same shape as `images` all the way through persistence,
    the SSE done frame, and the UI. Nothing here generates more than one clip:
    at this price, `n` would be a footgun.

    Three provider round-trips, because a video is a job and not a value:
    submit, poll until terminal, then download the bytes. Never raises — a
    video is an enrichment on top of the normal text answer, not something
    worth failing the whole request over, exactly as with
    providers.generate_images_litellm. Every failure path returns [] and logs;
    the caller turns that into an honest note (see failed_note) rather than
    letting the user wonder where their video went.
    """
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return []

    model = video_generation_model()
    # The status/content calls need to be told the provider explicitly: they
    # take a bare job id, which carries no prefix to infer it from.
    provider = model.split("/", 1)[0].strip().lower() if "/" in model else "openai"

    # One deadline for the whole operation, set before anything is dispatched.
    # It bounds BOTH the number of polls and each individual provider call: the
    # poll count alone is not a wall-clock bound, and LiteLLM's own default is
    # 600s (unbounded for the download), so a single hung call could hold the
    # user's HTTP request for ten minutes past a ceiling documented as "the
    # worst case they can be made to sit through".
    deadline = time.monotonic() + _timeout_seconds()

    def _remaining() -> float:
        # Never zero or negative: a non-positive timeout is "no timeout" to some
        # HTTP clients, which is the opposite of what an expired deadline means.
        return max(0.1, deadline - time.monotonic())

    try:
        # Imported inside the try with everything else: an ImportError from a
        # broken/absent litellm would otherwise escape a function whose entire
        # contract with its callers is that it never raises.
        litellm = _litellm()
        job = litellm.video_generation(
            prompt=clean_prompt,
            model=model,
            seconds=video_generation_seconds(),
            size=video_generation_size(),
            timeout=_remaining(),
        )
    except Exception:
        logger.exception("video.generate_failed model=%s", model)
        return []

    job_id = _job_id_of(job)
    if not job_id:
        logger.warning("video.no_job_id model=%s", model)
        return []

    status = _status_of(job)
    while status not in _DONE_STATUSES:
        if status in _FAILED_STATUSES:
            logger.warning(
                "video.job_failed model=%s status=%s error=%s",
                model,
                status,
                getattr(job, "error", None),
            )
            return []
        if time.monotonic() >= deadline:
            # Deliberately not a raise: the answer text is already written and
            # worth returning. The caller says so out loud instead.
            logger.warning("video.timed_out model=%s job=%s", model, job_id)
            return []
        # Never sleep past the deadline: waiting a full interval only to fail
        # the check on the next pass is time the user spends for nothing.
        time.sleep(min(_POLL_INTERVAL_SECONDS, _remaining()))
        try:
            job = litellm.video_status(
                video_id=job_id,
                custom_llm_provider=provider,
                timeout=_remaining(),
            )
        except Exception:
            logger.exception("video.status_failed model=%s job=%s", model, job_id)
            return []
        status = _status_of(job)

    try:
        content = litellm.video_content(
            video_id=job_id, custom_llm_provider=provider, timeout=_remaining()
        )
    except Exception:
        logger.exception("video.download_failed model=%s job=%s", model, job_id)
        return []

    if not isinstance(content, bytes) or not content:
        logger.warning("video.empty_content model=%s job=%s", model, job_id)
        return []
    if len(content) > _MAX_VIDEO_BYTES:
        # Skipped rather than truncated: half an MP4 is not a shorter video,
        # it is a broken file that would render as a silent failure in the UI.
        logger.warning(
            "video.too_large model=%s job=%s bytes=%d", model, job_id, len(content)
        )
        return []

    encoded = base64.b64encode(content).decode("ascii")
    return [f"data:video/mp4;base64,{encoded}"]


# --- the notes ----------------------------------------------------------------


def generated_note() -> str:
    return "Generated a video for this request."


def failed_note(model: str) -> str:
    """Said out loud for the same reason the image path says it: the user asked
    for a video, got prose, and the answering model — never told a call had been
    made on its behalf, let alone that it failed — can only guess when asked
    where the video went. Observed on the image path before it said this; there
    is no reason to relearn it here."""
    return (
        f"The video couldn't be generated ({model} returned nothing, failed, or "
        "took longer than VIDEO_GENERATION_TIMEOUT allows). The answer above is "
        "text only."
    )


def ground_truth_for(enabled: bool, generating: bool) -> str:
    """What to tell the answering model about video on THIS turn, or "" when
    there is nothing worth spending tokens to say.

    The image path learned this the expensive way: asked to draw the same thing
    twice in one conversation, it said "image generation is enabled here" once
    and "I can't generate images" once, both as fact, one necessarily wrong. The
    model cannot see a standalone call made alongside its own, so anything it
    says about one is a guess unless it is told. Same failure is available here,
    so the same fix ships with the feature rather than after it.
    """
    if generating:
        return (
            "VIDEO GROUND TRUTH FOR THIS TURN: a video generator is running on "
            "this question, in parallel with your answer, and its result will be "
            "attached to your reply automatically. Do NOT say you are unable to "
            "make videos, and do NOT describe what the video shows — you cannot "
            "see it. Write the text part of the answer only."
        )
    if not enabled:
        return (
            "VIDEO GROUND TRUTH FOR THIS TURN: the user has asked for a video. "
            "Video generation is a SETTING in this app (VIDEO_GENERATION) and it "
            "is switched OFF in this deployment — say that it is off and can be "
            "switched on, rather than that you are incapable of it."
        )
    return ""
