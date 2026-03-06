#!/usr/bin/env python3
"""Project task runner.

Usage:
    python do.py <command>

Commands:
    test        Run all tests
    presubmit   Run formatting, linting, tests, and memory size checks
"""

import os
import subprocess
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def run(cmd, **kwargs):
    """Run a command and return the result."""
    print(f"  -> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, cwd=ROOT_DIR, shell=isinstance(cmd, str), **kwargs)
    return result.returncode


def cmd_test():
    """Run all tests."""
    return run([sys.executable, "-m", "pytest", "tests/", "-v"])


def cmd_presubmit():
    """Run all presubmit checks."""
    checks = [
        ("tests", [sys.executable, "-m", "pytest", "tests/", "-v"]),
    ]

    failed = []
    for name, cmd in checks:
        print(f"\n== {name} ==")
        rc = run(cmd)
        if rc != 0:
            failed.append(name)

    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1

    print("\nAll presubmit checks passed.")
    return 0


def main():
    commands = {
        "test": cmd_test,
        "presubmit": cmd_presubmit,
    }

    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"Usage: python do.py <{'|'.join(commands.keys())}>")
        sys.exit(1)

    sys.exit(commands[sys.argv[1]]())


if __name__ == "__main__":
    main()
