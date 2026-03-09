#!/usr/bin/env python3
"""Project task runner.

Usage:
    ./do <command>

Commands:
    setup       Install all dev dependencies
    build       Build for release
    test        Run all tests
    lint        Run all linters
    format      Run all formatters
    coverage    Run all coverage tools
    presubmit   Run setup, formatters, linters, tests, coverage, then ci
    ci          Run all presubmit checks as CI would (temporary commit)
"""

import json
import os
import re
import signal
import subprocess
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT_DIR, ".workflow.jsonc")
PRESUBMIT_TIMEOUT_SECONDS = 10 * 60


def load_config():
    """Read and parse .workflow.jsonc, stripping // comments."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        stripped = re.sub(r"//[^\n]*", "", raw)
        return json.loads(stripped)
    except Exception:
        return {}


def run(cmd, **kwargs):
    """Run a command and return the result."""
    print(f"  -> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, cwd=ROOT_DIR, shell=isinstance(cmd, str), **kwargs)
    return result.returncode


def cmd_setup():
    """Install all dev dependencies."""
    print("setup: no dev dependencies configured")
    return 1


def cmd_build():
    """Build for release."""
    print("build: no build configured")
    return 1


def cmd_test():
    """Run all tests."""
    return run([sys.executable, "-m", "pytest", "tests/", "-v"])


def cmd_lint():
    """Run all linters."""
    print("lint: no linters configured")
    return 1


def cmd_format():
    """Run all formatters."""
    print("format: no formatters configured")
    return 1


def cmd_coverage():
    """Run tests with coverage reporting."""
    return run([
        sys.executable, "-m", "pytest", "tests/", "-v",
        "--cov", "--cov-report=term-missing",
    ])


def cmd_ci():
    """Run CI checks via .tools/ci.py."""
    ci_script = os.path.join(ROOT_DIR, ".tools", "ci.py")
    if not os.path.exists(ci_script):
        print("ci: .tools/ci.py not found")
        return 1
    return run([sys.executable, ci_script])


def cmd_presubmit():
    """Run all presubmit checks with a timeout."""
    checks = [
        ("setup", cmd_setup), # Setup dependencies so presubmit passes in a clean checkout.
        ("format", cmd_format),
        ("lint", cmd_lint),
        ("build", cmd_build), # Verify build release works as part of presubmit.
        ("test", cmd_test),
        ("coverage", cmd_coverage),
    ]

    def _timeout_handler(signum, frame):
        print(f"\nPresubmit timed out after {PRESUBMIT_TIMEOUT_SECONDS // 60} minutes.")
        sys.exit(1)

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(PRESUBMIT_TIMEOUT_SECONDS)

    failed = []
    for name, fn in checks:
        print(f"\n== {name} ==")
        rc = fn()
        if rc != 0:
            failed.append(name)

    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)

    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1

    print("\nAll presubmit checks passed. Running ci...")
    return cmd_ci()


def main():
    commands = {
        "setup": cmd_setup,
        "build": cmd_build,
        "test": cmd_test,
        "lint": cmd_lint,
        "format": cmd_format,
        "coverage": cmd_coverage,
        "presubmit": cmd_presubmit,
        "ci": cmd_ci,
    }

    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"Usage: ./do <{'|'.join(commands.keys())}>")
        sys.exit(1)

    sys.exit(commands[sys.argv[1]]())


if __name__ == "__main__":
    main()
