#!/usr/bin/env python3
"""One command that runs every check CI runs, in CI's own order.

    python scripts/verify.py

Exists because "all the tests pass" used to mean whichever suites you
happened to remember. The backend suite, the frontend suite and the
Playwright E2E suite each live behind a different runner in a different
directory, and the E2E suite additionally needs the frontend BUILT first
(``vite preview`` serves ``frontend/dist``, not the dev server) -- so it is
the one that is easiest to skip by accident and the one most likely to be
the thing CI goes red on. A single entry point removes the judgement call:
"verified" means this script exited 0.

Deliberately a superset of nothing and a subset of nothing -- every step
below mirrors a step in .github/workflows/ci.yml exactly (same commands,
same flags, same coverage gates), so a green run here and a green run there
mean the same thing. Adding a check to CI means adding it here too.

`--only <group>` (backend/frontend/e2e, repeatable) narrows the run while
iterating. It is NOT a way to declare something verified: a partial run says
so, loudly, in its summary and never claims otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GROUPS = ("backend", "frontend", "e2e")


def _python_bin() -> str:
    """The repo's venv interpreter when there is one, else the interpreter
    running this script. Mirrors e2e/playwright.config.ts's identical
    venv-or-bare-interpreter probe -- a local checkout has a venv with every
    dep installed; CI installs into the runner's system Python instead."""
    venv = (
        REPO_ROOT
        / "venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    return str(venv) if venv.exists() else sys.executable


# npm/npx are .cmd shims on Windows and subprocess (shell=False) will not
# find the bare name there.
_NPM = "npm.cmd" if sys.platform == "win32" else "npm"
_NPX = "npx.cmd" if sys.platform == "win32" else "npx"


@dataclass
class Step:
    group: str
    name: str
    cmd: list[str]
    cwd: Path
    # Steps that must have passed for this one to be meaningful. The E2E
    # suite serves frontend/dist, so a failed build makes it a test of stale
    # bytes -- reported as SKIPPED (never as passing).
    requires: tuple[str, ...] = ()
    status: str = "pending"
    seconds: float = 0.0


@dataclass
class Result:
    steps: list[Step] = field(default_factory=list)

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.status == "FAILED"]

    @property
    def skipped(self) -> list[Step]:
        return [s for s in self.steps if s.status == "SKIPPED"]


def _steps(python: str) -> list[Step]:
    frontend = REPO_ROOT / "frontend"
    e2e = REPO_ROOT / "e2e"
    return [
        Step(
            "backend",
            "ruff check",
            [python, "-m", "ruff", "check", "app", "tests", "evals", "scripts"],
            REPO_ROOT,
        ),
        Step(
            "backend",
            "ruff format --check",
            [python, "-m", "ruff", "format", "--check", "app", "tests", "evals", "scripts"],
            REPO_ROOT,
        ),
        Step("backend", "mypy", [python, "-m", "mypy"], REPO_ROOT),
        # The fail_under gate lives in pyproject.toml's [tool.coverage.report]
        # and coverage.py auto-discovers it -- no extra flag needed here.
        Step(
            "backend",
            "pytest (+coverage gate)",
            [
                python,
                "-m",
                "pytest",
                "tests",
                "-q",
                "--cov=app",
                "--cov-report=term-missing",
            ],
            REPO_ROOT,
        ),
        Step("frontend", "eslint", [_NPM, "run", "lint"], frontend),
        # test:coverage, not test -- the thresholds in vitest.config.ts only
        # run under the coverage reporter.
        Step(
            "frontend",
            "vitest (+coverage gate)",
            [_NPM, "run", "test:coverage"],
            frontend,
        ),
        # tsc -b && vite build. Also produces the frontend/dist the E2E
        # suite's `vite preview` serves, which is why it runs before it.
        Step("frontend", "build (tsc + vite)", [_NPM, "run", "build"], frontend),
        Step("e2e", "tsc --noEmit", [_NPM, "run", "typecheck"], e2e),
        Step(
            "e2e",
            "playwright test",
            [_NPX, "playwright", "test"],
            e2e,
            requires=("frontend/build (tsc + vite)",),
        ),
    ]


def _preflight(steps: list[Step]) -> list[str]:
    """Hard-fail on a missing prerequisite rather than letting a step fail
    with an inscrutable runner error -- an absent node_modules must never
    read as "the suite didn't apply here"."""
    problems: list[str] = []
    groups = {s.group for s in steps}
    if "frontend" in groups and not (REPO_ROOT / "frontend" / "node_modules").is_dir():
        problems.append(
            "frontend/node_modules is missing - run: cd frontend && npm install"
        )
    if "e2e" in groups and not (REPO_ROOT / "e2e" / "node_modules").is_dir():
        problems.append("e2e/node_modules is missing - run: cd e2e && npm install")
    return problems


def _run(step: Step, done: dict[str, str]) -> None:
    key = f"{step.group}/{step.name}"
    blocked = [r for r in step.requires if done.get(r) != "PASSED"]
    if blocked:
        step.status = "SKIPPED"
        print(
            f"\n>>> SKIP  {key} - depends on {', '.join(blocked)}, which did not pass",
            flush=True,
        )
        done[key] = step.status
        return

    print(f"\n>>> RUN   {key}\n    $ {' '.join(step.cmd)}  (in {step.cwd})", flush=True)
    started = time.monotonic()
    completed = subprocess.run(step.cmd, cwd=step.cwd)  # noqa: S603 - fixed argv, no shell
    step.seconds = time.monotonic() - started
    step.status = "PASSED" if completed.returncode == 0 else "FAILED"
    print(f"<<< {step.status:6} {key} ({step.seconds:.1f}s)", flush=True)
    done[key] = step.status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=GROUPS,
        help="run just this group (repeatable). A partial run is never a full verification.",
    )
    args = parser.parse_args(argv)
    selected = tuple(dict.fromkeys(args.only)) if args.only else GROUPS
    partial = selected != GROUPS

    python = _python_bin()
    steps = [s for s in _steps(python) if s.group in selected]

    problems = _preflight(steps)
    if problems:
        print("Cannot verify - missing prerequisites:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    print(f"verify.py - {len(steps)} step(s) across: {', '.join(selected)}")
    print(f"python: {python}")

    result = Result(steps)
    done: dict[str, str] = {}
    for step in steps:
        _run(step, done)

    print("\n" + "=" * 68)
    for step in steps:
        print(f"  {step.status:8} {step.group}/{step.name}  ({step.seconds:.1f}s)")
    print("=" * 68)

    if result.failed or result.skipped:
        print(
            f"\nNOT VERIFIED - {len(result.failed)} failed, {len(result.skipped)} skipped."
        )
        return 1
    if partial:
        print(
            f"\nPARTIAL RUN ({', '.join(selected)}) - passing, but this is NOT a full verification."
        )
        print(
            "Run `python scripts/verify.py` with no --only before calling the work done."
        )
        return 0
    print("\nVERIFIED - every check CI runs passed locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
