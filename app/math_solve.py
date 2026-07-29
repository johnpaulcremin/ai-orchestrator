"""Optional precision math tool: exact symbolic/numeric algebra and calculus
via SymPy, offered to the model as a function/custom tool (OpenAI Responses
API `function`, Anthropic Messages API custom tool-use — same shared-schema
pattern as app/actions.py's propose_action) so the model can get a VERIFIED
result instead of computing one itself and possibly getting it wrong.

Unlike propose_action, this needs no user confirmation: SymPy computation
has no real-world side effects, so a call is executed immediately, server-
side, the moment it's extracted from the model's response — no separate
confirm/decline endpoint, no persisted "pending" state. Structurally closer
to code_execution (auto-run, result folded straight into the answer) than
to actions, just without a hosted sandbox: this app's own process runs the
computation directly, in-process, no subprocess/container involved.

Genuinely different from code_execution: that relies on the MODEL writing
correct Python and a hosted sandbox running it — useful for arbitrary
verification, but the code itself could still be wrong. This is
deterministic: SymPy either solves the expression exactly, falls back to an
external solver, or reports it can't — with zero chance of a silently-wrong
"looks right" answer the way model-generated code can produce. Zero LLM
tokens spent on the computation itself; zero external API calls in the
common case (SymPy alone handles it).

OPTIONAL WOLFRAM ALPHA FALLBACK: when SymPy fails to parse or compute an
expression that has already passed every safety check below, and
WOLFRAM_ALPHA_APP_ID is configured, solve_math() falls back to Wolfram
Alpha's Short Answers API for expressions SymPy can't handle (e.g. some
transcendental equations, closed forms SymPy doesn't know). The result's
`source` field ("sympy" or "wolfram_alpha") tells the caller which engine
actually produced it. Entirely optional — solve_math() works the same as
before with no key configured, just without this fallback.

SECURITY: `expression` arrives from model output, which can in turn be
steered by adversarial user/document/tool-result text (prompt injection).
SymPy's expression parser evaluates the input via a *restricted* namespace,
but that restriction is a defense that has had bypasses historically, not
an absolute guarantee — so this module never relies on the parser's own
sandboxing alone. Three independent layers, all of which must fail for
anything unsafe to execute: (1) a strict character allowlist (no quotes,
brackets, backticks, semicolons — the entire string-literal-based Python
injection surface requires a quote character, which is never needed for a
real math expression and is flatly rejected here), (2) a keyword denylist
(import/exec/eval/lambda/os./sys./open(/__ and friends), and (3) parsing
with an explicit namespace that strips `__builtins__` and exposes only
SymPy's own public names — SymPy's default parser behavior if left
unspecified. See test_math_solve.py's injection-attempt tests for the
concrete attacks this was built to withstand.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
import sympy
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

from .telemetry import logger

MATH_SOLVE_TOOL_DESCRIPTION = (
    "Get an exact, verified result for an algebra or calculus question via "
    "a symbolic math engine (SymPy), instead of computing it yourself and "
    "risking an error. Use this whenever the user's question reduces to a "
    "concrete symbolic/numeric computation: solving an equation, "
    "simplifying an expression, differentiating, integrating, or "
    "evaluating a numeric expression exactly. Write `expression` in "
    "standard math notation (e.g. 'x**2 - 4', '2*x + 3*y', 'sin(x)*cos(x)', "
    "'sqrt(16)') — no quotes, string literals, or code, just the math."
)

_OPERATIONS = ("solve", "simplify", "differentiate", "integrate", "evaluate")


def math_solve_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": list(_OPERATIONS),
                "description": (
                    "'solve' an equation for `variable` (e.g. 'x**2 - 4' or "
                    "'x**2 = 4'); 'simplify' an expression; 'differentiate' "
                    "or 'integrate' with respect to `variable`; 'evaluate' "
                    "a purely numeric expression exactly (fractions kept "
                    "exact, not rounded)."
                ),
            },
            "expression": {
                "type": "string",
                "description": "The math expression, in standard notation. No quotes or code.",
            },
            "variable": {
                "type": "string",
                "description": "The symbol to solve/differentiate/integrate with respect to. Default 'x'.",
            },
        },
        "required": ["operation", "expression"],
    }


# The entire attack surface for a Python-eval-based parser is string
# literals (no math expression legitimately needs one) — this allowlist
# excludes quotes, brackets, backticks, semicolons, colons, and every other
# character not needed for algebra/calculus notation.
_ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9_\s+\-*/^().,=<>!]+$")
_MAX_EXPRESSION_LENGTH = 200
_DENYLIST_SUBSTRINGS = (
    "__",
    "import",
    "exec",
    "eval",
    "lambda",
    "os.",
    "sys.",
    "open(",
    "globals",
    "locals",
    "compile",
    "getattr",
    "setattr",
    "delattr",
    "class ",
    "def ",
)

_TRANSFORMATIONS = standard_transformations + (implicit_multiplication_application,)

# SymPy's own public namespace, with Python's builtins explicitly stripped —
# passed as parse_expr's global_dict so evaluation can never reach an actual
# __import__/open/exec even if every other layer above were bypassed.
_SAFE_GLOBALS: dict[str, Any] = {
    name: value for name, value in vars(sympy).items() if not name.startswith("_")
}
_SAFE_GLOBALS["__builtins__"] = {}


def wolfram_alpha_configured() -> bool:
    """True if WOLFRAM_ALPHA_APP_ID is set. Optional: solve_math works fully
    on SymPy alone without it, same convention as GOOGLE_FACT_CHECK_API_KEY —
    presence of the key is the on/off switch, no separate feature flag."""
    return bool((os.getenv("WOLFRAM_ALPHA_APP_ID") or "").strip())


def _wolfram_alpha_query(expression: str) -> str | None:
    """A plain-text answer for `expression` from Wolfram Alpha's Short
    Answers API, or None on any failure (not configured, timeout, no answer,
    HTTP error) — a best-effort fallback, never a hard dependency.

    Only ever called from solve_math's `except` branch, i.e. on an
    expression that ALREADY passed every safety check above and still failed
    to parse/compute in SymPy — never on a security-rejected expression, so
    there is nothing further to sanitize before sending it to an external
    API.
    """
    app_id = (os.getenv("WOLFRAM_ALPHA_APP_ID") or "").strip()
    if not app_id:
        return None
    try:
        response = httpx.get(
            "https://api.wolframalpha.com/v1/result",
            params={"appid": app_id, "i": expression},
            timeout=8.0,
        )
    except httpx.HTTPError:
        logger.warning("math_solve.wolfram_alpha_failed", exc_info=True)
        return None
    if response.status_code != 200:
        return None
    text = response.text.strip()
    return text or None


def _reject_reason(expression: str) -> str | None:
    """None if `expression` passes every safety layer, else why it didn't."""
    if not expression or not expression.strip():
        return "empty expression"
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        return f"expression longer than {_MAX_EXPRESSION_LENGTH} characters"
    if not _ALLOWED_CHARS.match(expression):
        return "expression contains characters not valid in a math expression"
    lowered = expression.lower()
    for marker in _DENYLIST_SUBSTRINGS:
        if marker in lowered:
            return "expression contains a disallowed keyword"
    return None


def _parse(expression: str, variable: str) -> Any:
    symbol = sympy.Symbol(variable)
    local_dict = {variable: symbol}
    return parse_expr(
        expression,
        local_dict=local_dict,
        global_dict=_SAFE_GLOBALS,
        transformations=_TRANSFORMATIONS,
    )


def solve_math(
    operation: str, expression: str, variable: str = "x"
) -> dict[str, object]:
    """Compute `operation` on `expression`, or an {"error": ...} entry if
    the expression fails a safety check, doesn't parse, or the operation
    itself fails (e.g. an unsolvable/malformed equation). Never raises —
    this is offered as a tool result, not a hard dependency for answering.
    """
    clean_operation = (operation or "").strip().lower()
    clean_expression = (expression or "").strip()
    clean_variable = (variable or "x").strip() or "x"
    base: dict[str, object] = {
        "operation": clean_operation,
        "expression": clean_expression,
        "variable": clean_variable,
    }

    if clean_operation not in _OPERATIONS:
        return {**base, "error": f"unknown operation '{operation}'"}

    reason = _reject_reason(clean_expression)
    if reason is not None:
        return {**base, "error": reason}
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean_variable):
        return {**base, "error": f"invalid variable name '{variable}'"}

    try:
        if clean_operation == "solve":
            # An equation may be written as "lhs = rhs"; a bare expression
            # is treated as already-equals-zero.
            if "=" in clean_expression and "==" not in clean_expression:
                lhs_text, rhs_text = clean_expression.split("=", 1)
                lhs = _parse(lhs_text, clean_variable)
                rhs = _parse(rhs_text, clean_variable)
                target = lhs - rhs
            else:
                target = _parse(clean_expression, clean_variable)
            result = sympy.solve(target, sympy.Symbol(clean_variable))
        elif clean_operation == "simplify":
            result = sympy.simplify(_parse(clean_expression, clean_variable))
        elif clean_operation == "differentiate":
            result = sympy.diff(
                _parse(clean_expression, clean_variable), sympy.Symbol(clean_variable)
            )
        elif clean_operation == "integrate":
            result = sympy.integrate(
                _parse(clean_expression, clean_variable), sympy.Symbol(clean_variable)
            )
        else:  # "evaluate"
            result = _parse(clean_expression, clean_variable).evalf()
    except Exception as exc:
        logger.warning(
            "math_solve.compute_failed op=%s", clean_operation, exc_info=True
        )
        fallback = _wolfram_alpha_query(clean_expression)
        if fallback:
            return {**base, "result": fallback, "source": "wolfram_alpha"}
        return {**base, "error": f"could not compute: {exc}"}

    return {**base, "result": str(result), "source": "sympy"}


def note(result: dict[str, object]) -> str:
    if result.get("error"):
        return f"Tried to compute this exactly but couldn't: {result['error']}."
    if result.get("source") == "wolfram_alpha":
        return (
            "SymPy couldn't solve this exactly; computed via Wolfram Alpha "
            f"instead: **{result.get('result')}**"
        )
    return f"Computed exactly with SymPy: **{result.get('result')}**"
