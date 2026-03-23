#!/usr/bin/env python3
"""
Presubmit harness — mounted read-only at /harness.py inside agent containers.

This script is the authoritative verification gate. It cannot be modified by
agents. All thresholds, lint checks, and verification steps are hardcoded here.
Agents CANNOT relax requirements by editing any workspace file.

Usage:
    python3 /harness.py              # full presubmit
    python3 /harness.py --setup-only # run setup only (dependency bootstrap)

Each step calls the matching hook from .agent/harness_hooks.py which performs
the actual tool invocations (cargo, npm, pytest, etc.). The harness then
validates results: exit codes, structured lint output, and coverage reports
against hardcoded thresholds.

Steps (in order):
    1. setup        — hook "setup" (installs deps; agents may customise)
    2. fmt          — hook "fmt" (cargo fmt, prettier); harness validates exit code
    3. lint         — hook "lint" (clippy, cargo-deny, cargo-lock, eslint);
                      harness parses target/lint-results.json for failures
    4. python-tests — hook "test" (pytest); harness validates exit code
    5. build        — hook "build" (cargo build, npm build); harness validates exit code
    6. coverage     — hook "coverage" (llvm-cov, c8); harness validates thresholds:
                        unit: 90% line coverage
                        E2E:  70% line coverage
                        Node: 90% line coverage (frontend c8)
"""

import json
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Hardcoded thresholds — agents CANNOT relax these
# ---------------------------------------------------------------------------
UNIT_COVERAGE_THRESHOLD = 90.0
E2E_COVERAGE_THRESHOLD = 70.0
NODE_COVERAGE_THRESHOLD = 90.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_SCRIPT = os.path.join(os.getcwd(), ".agent", "harness_hooks.py")
LINT_RESULT_PATH = "target/lint-results.json"


def _run_hook(step: str, *, check: bool = True) -> int:
    """Call .agent/harness_hooks.py <step> and return the exit code.

    If check=True (default), a non-zero exit code terminates the harness.
    If check=False, the caller is responsible for interpreting the exit code.
    """
    if not os.path.isfile(HOOKS_SCRIPT):
        print(f"[HARNESS] WARNING: hooks script not found at {HOOKS_SCRIPT}",
              file=sys.stderr)
        return 1

    print(f"[HARNESS] hook: running {HOOKS_SCRIPT} {step}")
    result = subprocess.run(
        [sys.executable, HOOKS_SCRIPT, step],
        env=os.environ,
    )
    if check and result.returncode != 0:
        print(f"[HARNESS] FAILED: hook '{step}' exited {result.returncode}",
              file=sys.stderr)
        sys.exit(result.returncode)
    return result.returncode


# ---------------------------------------------------------------------------
# Step 1: Setup
# ---------------------------------------------------------------------------

def step_setup() -> None:
    _run_hook("setup")
    print("[HARNESS] setup: OK")


# ---------------------------------------------------------------------------
# Step 2: Format check
# ---------------------------------------------------------------------------

def step_fmt() -> None:
    _run_hook("fmt")
    print("[HARNESS] fmt: OK")


# ---------------------------------------------------------------------------
# Step 3: Lint (hook runs tools, harness parses structured results)
# ---------------------------------------------------------------------------

def step_lint() -> None:
    rc = _run_hook("lint", check=False)

    # Parse structured lint results written by the hook
    failures: list[str] = []
    if os.path.isfile(LINT_RESULT_PATH):
        try:
            with open(LINT_RESULT_PATH) as f:
                data = json.load(f)
            failures = data.get("failures", [])
        except Exception as exc:
            print(f"[HARNESS] lint: WARNING — failed to parse {LINT_RESULT_PATH}: {exc}",
                  file=sys.stderr)

    if failures:
        print(f"[HARNESS] lint: {len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)

    if rc != 0:
        print("[HARNESS] lint: hook exited non-zero but produced no structured failures",
              file=sys.stderr)
        sys.exit(rc)

    print("[HARNESS] lint: OK")


# ---------------------------------------------------------------------------
# Step 4: Python tests
# ---------------------------------------------------------------------------

def step_python_tests() -> None:
    _run_hook("test")
    print("[HARNESS] python-tests: OK")


# ---------------------------------------------------------------------------
# Step 5: Build
# ---------------------------------------------------------------------------

def step_build() -> None:
    _run_hook("build")
    print("[HARNESS] build: OK")


# ---------------------------------------------------------------------------
# Step 6: Coverage (hook runs tools, harness validates thresholds)
# ---------------------------------------------------------------------------

def _parse_coverage_json(
    path: str, threshold: float, name: str
) -> tuple[float, list[str], list[str]]:
    errors: list[str] = []

    if not os.path.exists(path):
        errors.append(
            f"[HARNESS] coverage: ERROR — {name} report not found at {path}"
        )
        return 0.0, [], errors

    try:
        with open(path) as f:
            data = json.load(f)

        line_pct: float = data["data"][0]["totals"]["lines"]["percent"]
        total_lines: int = data["data"][0]["totals"]["lines"].get("count", 0)

        files_below = [
            fe["filename"]
            for fe in data["data"][0].get("files", [])
            if fe["summary"]["lines"]["percent"] < threshold
        ]

        if line_pct < threshold:
            errors.append(
                f"[HARNESS] coverage: ERROR — {name} line coverage {line_pct:.1f}%"
                f" is below the hardcoded {threshold}% threshold"
            )

        return line_pct, files_below, errors

    except Exception as exc:
        errors.append(
            f"[HARNESS] coverage: ERROR — failed to parse {path}: {exc}"
        )
        return 0.0, [], errors


def _parse_node_coverage_json(
    path: str, threshold: float,
) -> tuple[float, list[str], list[str]]:
    """Parse Istanbul/c8 coverage-final.json (keyed by file path)."""
    errors: list[str] = []

    if not os.path.exists(path):
        errors.append(
            f"[HARNESS] coverage: ERROR — Node report not found at {path}"
        )
        return 0.0, [], errors

    try:
        with open(path) as f:
            data = json.load(f)

        total_stmts = 0
        covered_stmts = 0
        files_below: list[str] = []

        for filepath, entry in data.items():
            s = entry.get("s", {})
            file_total = len(s)
            file_covered = sum(1 for v in s.values() if v > 0)
            total_stmts += file_total
            covered_stmts += file_covered
            if file_total > 0 and (file_covered / file_total * 100) < threshold:
                files_below.append(filepath)

        line_pct = (covered_stmts / total_stmts * 100) if total_stmts > 0 else 0.0

        if line_pct < threshold:
            errors.append(
                f"[HARNESS] coverage: ERROR — Node statement coverage {line_pct:.1f}%"
                f" is below the hardcoded {threshold}% threshold"
            )

        return line_pct, files_below, errors

    except Exception as exc:
        errors.append(
            f"[HARNESS] coverage: ERROR — failed to parse {path}: {exc}"
        )
        return 0.0, [], errors


def step_coverage() -> None:
    # Run the hook — it executes llvm-cov and c8, producing reports
    _run_hook("coverage", check=False)

    # Threshold validation — hardcoded, cannot be changed by agents
    u_pct, u_files, u_errors = _parse_coverage_json(
        "target/coverage/unit.json", UNIT_COVERAGE_THRESHOLD, "Unit"
    )
    e_pct, e_files, e_errors = _parse_coverage_json(
        "target/coverage/e2e.json", E2E_COVERAGE_THRESHOLD, "E2E"
    )
    n_pct, n_files, n_errors = _parse_node_coverage_json(
        "target/coverage/node/coverage-final.json", NODE_COVERAGE_THRESHOLD,
    )

    print(f"[HARNESS] coverage: Unit {u_pct:.1f}% (threshold {UNIT_COVERAGE_THRESHOLD}%)")
    print(f"[HARNESS] coverage: E2E  {e_pct:.1f}% (threshold {E2E_COVERAGE_THRESHOLD}%)")
    print(f"[HARNESS] coverage: Node {n_pct:.1f}% (threshold {NODE_COVERAGE_THRESHOLD}%)")

    for fname in u_files:
        print(f"  [unit below threshold] {fname}", file=sys.stderr)
    for fname in e_files:
        print(f"  [e2e  below threshold] {fname}", file=sys.stderr)
    for fname in n_files:
        print(f"  [node below threshold] {fname}", file=sys.stderr)

    all_errors = u_errors + e_errors + n_errors
    if all_errors:
        for err in all_errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    print("[HARNESS] coverage: OK")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_STEPS = [
    ("setup",        step_setup),
    ("fmt",          step_fmt),
    ("lint",         step_lint),
    ("python-tests", step_python_tests),
    ("build",        step_build),
    ("coverage",     step_coverage),
]


def main() -> None:
    setup_only = "--setup-only" in sys.argv

    t0 = time.monotonic()
    print(f"[HARNESS] presubmit started (setup_only={setup_only})")

    for name, fn in _STEPS:
        if setup_only and name != "setup":
            continue
        fn()

    elapsed = time.monotonic() - t0
    print(f"[HARNESS] presubmit OK ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
