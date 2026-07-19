"""Quick sanity check of the orchestrator's environment configuration.

Run with:  python check_env.py  (or venv/Scripts/python.exe check_env.py)
Reports what is configured and warns about the most common misconfiguration —
tier models that collapse to one, which makes routing a no-op.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _val(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def main() -> None:
    print("=== AI Orchestrator environment ===")

    key = _val("OPENAI_API_KEY")
    print(f"OPENAI_API_KEY:      {'set' if key else 'MISSING (required)'}")
    if _val("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY:   set (Claude models enabled)")

    base = _val("OPENAI_MODEL", "gpt-5")
    router = _val("OPENAI_MODEL_ROUTER", "gpt-5-nano")
    fast = _val("OPENAI_MODEL_FAST", base)
    smart = _val("OPENAI_MODEL_SMART", base)
    fallback = _val("OPENAI_MODEL_FALLBACK", base)

    print("\nModel tiers:")
    print(f"  router:   {router}")
    print(f"  fast:     {fast}")
    print(f"  smart:    {smart}")
    print(f"  fallback: {fallback}")

    warnings = []
    if fast == smart:
        warnings.append(
            "fast and smart tiers resolve to the same model — routing is a no-op "
            "that still pays for a classifier call on every auto request."
        )
    if fallback == smart:
        warnings.append(
            "fallback equals the smart tier — a model-specific outage cannot fall back."
        )
    if not key:
        warnings.append("OPENAI_API_KEY is required (also used by the auto router).")

    auth_bits = []
    if _val("JWT_SECRET"):
        auth_bits.append("JWT accounts")
    if _val("API_AUTH_TOKEN"):
        auth_bits.append("static token")

    print("\nOptional features:")
    print(f"  auth:        {' + '.join(auth_bits) or 'disabled'}")
    print(f"  rate limit:  {_val('RATE_LIMIT') or 'off'}")
    otel = _val("OTEL_EXPORTER_OTLP_ENDPOINT")
    print(f"  tracing:     {'on -> ' + otel if otel else 'off'}")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  ! {warning}")
    else:
        print("\nNo issues detected.")


if __name__ == "__main__":
    main()
