Use AGENTS.md strictly.



First, run only:

git status --short

git diff --stat



Then make a short plan. Do not edit files yet.



Goal:

\[describe one specific feature or bug]



Constraints:

\- Keep the change minimal.

\- Touch only the smallest necessary files.

\- Do not modify frontend unless required.

\- Do not add dependencies.

\- Use rg/fd/ast-grep for search instead of scanning the repo.

\- Run only the relevant checks before finishing.



\## Verification



"Verified" means one unambiguous thing in this repo: `scripts/verify.py`
exited 0.

```
python scripts/verify.py
```

That runs, in CI's own order: ruff check, ruff format --check, mypy, pytest
with its coverage gate, eslint, vitest with its coverage gate, the frontend
build, the E2E type-check, and the Playwright E2E suite. It is a mirror of
`.github/workflows/ci.yml` — a green run here and a green CI run mean the
same thing. Add a check to CI, add it here too.

\### The frontend/E2E rule

\*\*Any change touching `frontend/` MUST run the Playwright E2E suite locally
and report its result before the work is declared done.\*\* Not "the unit
tests pass" — the E2E suite, actually run, with its pass/fail stated in the
summary.

This is a rule because it has already gone wrong: a session reported "558
tests passing, tsc/eslint clean, coverage above gates" and CI went red
anyway. Every one of those claims was true. None of them ran Playwright,
because the E2E suite is not part of any default local test command — it
lives behind its own runner in `e2e/`, and it serves `frontend/dist`, so it
also silently tests STALE bytes unless the frontend is rebuilt first.

`python scripts/verify.py` satisfies this rule on its own; it does the
build, then the E2E run, in that order. If you run the steps by hand
instead, the build step is not optional:

```
cd frontend && npm ci && npm run build
cd ../e2e && npm ci && npx playwright install --with-deps chromium && npx playwright test
```

(`npm ci` and `playwright install` are first-time-only.)

While iterating, `python scripts/verify.py --only backend` (or `frontend`,
or `e2e`) narrows the run. A partial run is never a verification — the
script says so itself, and the full command still has to pass before the
work is done.

\### What this rule does not license

Nothing here relaxes "Run only the relevant checks before finishing" for a
backend-only change — a change that does not touch `frontend/` still does
not need Playwright. And no existing check may be skipped, weakened, or
have its gate lowered to make a run go green: the coverage floors in
`pyproject.toml` and `frontend/vitest.config.ts` only ever ratchet up.
