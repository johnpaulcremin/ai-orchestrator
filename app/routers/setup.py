"""First-run setup: verifies a candidate API key with one minimal call and
reports the outcome, without ever storing, logging, or echoing the key.

The one endpoint here exists because a fresh install has no way to tell a
wrong key from a dead network from a working one until the first real
question fails — and that failure surfaces as an empty answer with a note,
which reads like the app is broken rather than unconfigured. The wizard in
the UI (frontend/src/SetupWizard.tsx) calls this before telling the operator
what line to put in .env.

What this deliberately does NOT do is save the key. app/settings.py states
the boundary: credential keys are absent from every settable-key tuple so
the settings API can never write, overwrite or read back a secret, and that
is enforced in three places. Writing .env from the app would also be a lie
by omission — load_dotenv() runs once at import and get_client() caches the
OpenAI client for the process, so a saved key would not take effect until a
restart anyway. The honest contract is therefore verify-then-instruct: test
the key the operator pasted, tell them exactly which variable to set and
that a restart is needed, and forget the key the moment the response is
sent.

The candidate key is used through a throwaway client constructed here,
never through orchestrator_calls.get_client(): that one is a process-wide
singleton bound to whatever OPENAI_API_KEY was at first use, and routing a
test through it would either test the wrong key or poison the cache.
"""

from __future__ import annotations

from fastapi import Depends, Request
from openai import APIConnectionError, BadRequestError, OpenAI

from ..auth import current_owner
from ..providers import AUTH_ERRORS, RATE_ERRORS, TIMEOUT_ERRORS
from ..ratelimit import limiter, rate_limit_value
from ..schemas import SetupTestKeyRequest, SetupTestKeyResponse
from ..settings import model_setting
from ..telemetry import logger
from .deps import router

# Short on purpose. A key check that hangs for the classifier's full budget
# is worse than one that reports "unreachable" quickly and lets the operator
# fix the network first.
_PROBE_TIMEOUT_SECONDS = 15.0
# Enough for the call to complete; the content of the answer is irrelevant.
_PROBE_MAX_OUTPUT_TOKENS = 32


def _probe(api_key: str, model: str) -> SetupTestKeyResponse:
    """One minimal call with `api_key`, classified into an outcome the wizard
    can act on. Every branch that reaches the provider and gets ANY reply
    other than an auth rejection counts as a working key — a model that
    rejects a parameter or is being throttled has already accepted the
    credential, which is the only thing being tested."""
    client = OpenAI(api_key=api_key).with_options(timeout=_PROBE_TIMEOUT_SECONDS)
    try:
        client.responses.create(
            model=model, input="ping", max_output_tokens=_PROBE_MAX_OUTPUT_TOKENS
        )
    except AUTH_ERRORS:
        return SetupTestKeyResponse(
            ok=False,
            outcome="auth_failed",
            model=model,
            detail="The provider rejected this key. Check it was copied completely.",
        )
    except RATE_ERRORS:
        return SetupTestKeyResponse(
            ok=True,
            outcome="rate_limited",
            model=model,
            detail="The key works — the provider is throttling this account right now.",
        )
    except BadRequestError as exc:
        # The credential was accepted; the request shape was not (a model that
        # rejects max_output_tokens or the Responses API entirely). Still a
        # working key. The message is kept because "this key works but the
        # router model is wrong" is exactly what an operator needs to hear.
        return SetupTestKeyResponse(
            ok=True,
            outcome="ok",
            model=model,
            detail=f"The key works. The provider did not like the probe request: {exc.message}",
        )
    except (*TIMEOUT_ERRORS, APIConnectionError):
        return SetupTestKeyResponse(
            ok=False,
            outcome="unreachable",
            model=model,
            detail="Could not reach the provider. Check the network, a proxy, or OPENAI_BASE_URL.",
        )
    except Exception:  # noqa: BLE001 — every failure must become a verdict, never a 500
        # Logged without the key: the request body is never written out, and
        # this line carries only the model name.
        logger.exception("setup.test_key_failed model=%s", model)
        return SetupTestKeyResponse(
            ok=False,
            outcome="error",
            model=model,
            detail="The probe failed for a reason other than the key. See the server log.",
        )
    return SetupTestKeyResponse(
        ok=True, outcome="ok", model=model, detail="The key works."
    )


@router.post("/v1/setup/test-key", response_model=SetupTestKeyResponse)
@limiter.limit(rate_limit_value)
def test_key(
    request: Request,
    req: SetupTestKeyRequest,
    owner: str | None = Depends(current_owner),
):
    """Verify a candidate OPENAI_API_KEY with one cheap call. The key is used
    once, through a throwaway client, and never stored, logged, or returned.

    Probes the router model (OPENAI_MODEL_ROUTER) because it is the cheapest
    model the app is configured with and the one every auto-mode request
    needs anyway — if this key cannot reach it, nothing else will work either.
    """
    model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano")
    return _probe(req.api_key.strip(), model)
