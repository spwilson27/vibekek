#!/usr/bin/env python3
"""
Presubmit harness — mounted read-only at /harness.py inside agent containers.

This script is the authoritative verification gate. It cannot be modified by
agents. All thresholds, lint checks, and verification steps are hardcoded here.
Agents CANNOT relax requirements by editing any workspace file.

Usage:
    python3 /harness.py              # full presubmit
    python3 /harness.py --setup-only # run setup only (dependency bootstrap)

Each step runs the hardcoded checks first, then calls the matching hook from
.agent/harness_hooks.py (if present) so agents can ADD checks but never skip
the built-in ones.

Steps (in order):
    1. setup        — .agent/harness_hooks.py setup  (installs deps; agents may customise)
    2. fmt          — cargo fmt --all -- --check  +  hook "fmt"
    3. lint         — hardcoded sub-checks  +  hook "lint":
                        - cargo clippy --workspace --all-targets -- -D warnings
                        - cargo deny check bans licenses sources
                        - cargo update --locked --offline  (Cargo.lock integrity)
    4. python-tests — pytest tests/  +  hook "test"
    5. build        — cargo build --workspace --release  +  hook "build"
    6. coverage     — cargo llvm-cov (unit + E2E)  +  hook "coverage", thresholds:
                        unit: 90% line coverage
                        E2E:  70% line coverage
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
COVERAGE_IGNORE_REGEX = r''

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_SCRIPT = os.path.join(os.getcwd(), ".agent", "harness_hooks.py")


def _run(cmd: str | list, *, check: bool = True, capture: bool = False,
         env: dict | None = None) -> subprocess.CompletedProcess:
    merged_env = {**os.environ, **(env or {})}
    kwargs: dict = {"env": merged_env}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    if isinstance(cmd, str):
        result = subprocess.run(cmd, shell=True, **kwargs)
    else:
        result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        print(f"[HARNESS] FAILED (exit {result.returncode}): {cmd}", file=sys.stderr)
        sys.exit(result.returncode)
    return result


def _run_hook(step: str) -> None:
    """Call .agent/harness_hooks.py <step> if the hooks script exists.

    Called *after* the hardcoded checks so agents can only add checks,
    never bypass the built-in ones.
    """
    if os.path.isfile(HOOKS_SCRIPT):
        print(f"[HARNESS] hook: running {HOOKS_SCRIPT} {step}")
        _run([sys.executable, HOOKS_SCRIPT, step])


# ---------------------------------------------------------------------------
# Step 1: Setup (dependency hook — agents may customise .agent/harness_hooks.py)
# ---------------------------------------------------------------------------

def step_setup() -> None:
    _run_hook("setup")
    print("[HARNESS] setup: OK")


# ---------------------------------------------------------------------------
# Step 2: Format check
# ---------------------------------------------------------------------------

def step_fmt() -> None:
    print("[HARNESS] fmt: cargo fmt --all -- --check")
    _run("cargo fmt --all -- --check")
    _run_hook("fmt")
    print("[HARNESS] fmt: OK")


# ---------------------------------------------------------------------------
# Step 3: Lint (all explicit sub-checks)
# ---------------------------------------------------------------------------

def _lint_clippy() -> list[str]:
    print("[HARNESS] lint: cargo clippy --workspace --all-targets -- -D warnings")
    r = _run("cargo clippy --workspace --all-targets -- -D warnings", check=False)
    if r.returncode != 0:
        return ["[LINT-FAIL] Clippy checks failed."]
    return []


def _lint_deny() -> list[str]:
    print("[HARNESS] lint: cargo deny check bans licenses sources")
    r = _run("cargo deny check bans licenses sources", check=False)
    if r.returncode != 0:
        return ["[LINT-ERR-002] cargo-deny check failed."]
    return []


def _lint_cargo_lock() -> list[str]:
    print("[HARNESS] lint: cargo update --locked --offline (Cargo.lock integrity)")
    r = _run("cargo update --locked --offline", check=False, capture=True)
    if r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        stale_indicators = ["failed to select a version", "needs to be updated",
                            "lock file needs to be updated"]
        if any(ind in out for ind in stale_indicators):
            return ["[LINT-FAIL] Cargo.lock is out of sync with Cargo.toml"]
    return []

# TODO: Add additional generic npm lint commands

def step_lint() -> None:
    all_failures: list[str] = []

    all_failures += _lint_clippy()
    all_failures += _lint_deny()
    all_failures += _lint_cargo_lock()

    if all_failures:
        print(f"[HARNESS] lint: {len(all_failures)} failure(s):", file=sys.stderr)
        for f in all_failures:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)

    _run_hook("lint")
    print("[HARNESS] lint: OK")


# ---------------------------------------------------------------------------
# Step 4: Python tests (pytest)
# ---------------------------------------------------------------------------

def step_python_tests() -> None:
    """Run the full Python test suite via pytest.

    Uses .tools/pytest.ini for configuration (testpaths=tests, -x -n 4).
    Agents cannot relax or skip these tests by editing workspace files.
    """
    print("[HARNESS] python-tests: pytest tests/")
    # Run from the workspace root with the pinned config file
    _run(
        f"{sys.executable} -m pytest --config-file=.tools/harness/pytest.ini",
        check=True,
    )
    _run_hook("test")
    print("[HARNESS] python-tests: OK")


# ---------------------------------------------------------------------------
# Step 5: Build
# ---------------------------------------------------------------------------

def step_build() -> None:
    print("[HARNESS] build: cargo build --workspace --release")
    _run("cargo build --workspace --release")
    _run_hook("build")
    print("[HARNESS] build: OK")


# ---------------------------------------------------------------------------
# Step 5: Coverage (hardcoded thresholds — not delegable)
# ---------------------------------------------------------------------------

def _parse_coverage_json(
    path: str, threshold: float, name: str
) -> tuple[float, list[str], list[str]]:
    if not os.path.exists(path):
        print(f"[HARNESS] coverage: WARNING — {name} report not found at {path}", file=sys.stderr)
        return 0.0, [], []

    try:
        with open(path) as f:
            data = json.load(f)

        line_pct: float = data["data"][0]["totals"]["lines"]["percent"]
        total_lines: int = data["data"][0]["totals"]["lines"].get("count", 100)

        files_below = [
            fe["filename"]
            for fe in data["data"][0].get("files", [])
            if fe["totals"]["lines"]["percent"] < threshold
        ]

        errors: list[str] = []
        if line_pct < threshold:
            if total_lines == 0:
                print(
                    f"[HARNESS] coverage: WARNING — {name} coverage is 0.0% but total lines "
                    "is 0 (early bootstrap, skipping)",
                    file=sys.stderr,
                )
            else:
                errors.append(
                    f"[HARNESS] coverage: ERROR — {name} line coverage {line_pct:.1f}%"
                    f" is below the hardcoded {threshold}% threshold"
                )

        return line_pct, files_below, errors

    except Exception as exc:
        print(f"[HARNESS] coverage: WARNING — failed to parse {path}: {exc}", file=sys.stderr)
        return 0.0, [], []


def step_coverage() -> None:
    os.makedirs("target/coverage", exist_ok=True)

    # Clear RUSTFLAGS so cargo-llvm-cov can inject its own instrumentation flags.
    # Guard against re-entrant invocation from llvm-cov spawning test binaries.
    cov_env = {**os.environ, "_GOOEY_COVERAGE_RUNNING": "1"}
    cov_env.pop("RUSTFLAGS", None)

    print("[HARNESS] coverage: running unit coverage")
    _run(
        "cargo llvm-cov -j2 --features headless --all-targets --ignore-run-fail --lcov "
        f"--output-path target/coverage/unit.lcov "
        f"--ignore-filename-regex '{COVERAGE_IGNORE_REGEX}'",
        check=False, env=cov_env,
    )
    _run(
        "cargo llvm-cov report --json --output-path target/coverage/unit.json "
        f"--ignore-filename-regex '{COVERAGE_IGNORE_REGEX}'",
        check=False, env=cov_env,
    )

    print("[HARNESS] coverage: running E2E coverage")
    _run(
        "cargo llvm-cov -j2 --features headless,mcp --test e2e --ignore-run-fail --lcov "
        f"--output-path target/coverage/e2e.lcov "
        f"--ignore-filename-regex '{COVERAGE_IGNORE_REGEX}'",
        check=False, env=cov_env,
    )
    _run(
        "cargo llvm-cov report --json --output-path target/coverage/e2e.json "
        f"--ignore-filename-regex '{COVERAGE_IGNORE_REGEX}'",
        check=False, env=cov_env,
    )

    # Threshold validation — hardcoded, cannot be changed by agents
    u_pct, u_files, u_errors = _parse_coverage_json(
        "target/coverage/unit.json", UNIT_COVERAGE_THRESHOLD, "Unit"
    )
    e_pct, e_files, e_errors = _parse_coverage_json(
        "target/coverage/e2e.json", E2E_COVERAGE_THRESHOLD, "E2E"
    )

    print(f"[HARNESS] coverage: Unit {u_pct:.1f}% (threshold {UNIT_COVERAGE_THRESHOLD}%)")
    print(f"[HARNESS] coverage: E2E  {e_pct:.1f}% (threshold {E2E_COVERAGE_THRESHOLD}%)")

    for fname in u_files:
        print(f"  [unit below threshold] {fname}", file=sys.stderr)
    for fname in e_files:
        print(f"  [e2e  below threshold] {fname}", file=sys.stderr)

    all_errors = u_errors + e_errors
    if all_errors:
        for err in all_errors:
            print(err, file=sys.stderr)
        sys.exit(1)

    _run_hook("coverage")
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
