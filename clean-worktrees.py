#!/usr/bin/env python3
"""Clean up leftover task clone directories and ai-phase-* branches.

Task clones are created in the system temp directory with prefixes like
``ai_<task>_*`` or ``merge_<task>_*`` or ``serena_init_*``.  Failed tasks
leave their clone in place for inspection; this script removes them.
"""

import glob
import shutil
import subprocess
import sys
import tempfile


def run(args, **kwargs):
    return subprocess.run(args, **kwargs)


def check(args):
    return subprocess.run(args, capture_output=True)


def main():
    result = check(["git", "rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0:
        print("Error: Not a git repository.")
        sys.exit(1)

    tmp = tempfile.gettempdir()
    prefixes = ["ai_*", "merge_*", "serena_init_*"]

    print("Removing leftover task clone directories from temp...")
    for prefix in prefixes:
        for path in glob.glob(f"{tmp}/{prefix}"):
            print(f"  Removing: {path}")
            shutil.rmtree(path, ignore_errors=True)

    print("Deleting branches starting with 'ai-phase'...")
    result = check(["git", "branch", "--format=%(refname:short)", "--list", "ai-phase*"])
    branches = [b for b in result.stdout.decode().splitlines() if b.strip()]
    for branch in branches:
        print(f"  Deleting branch: {branch}")
        run(["git", "branch", "-D", branch])

    print("Running git prune...")
    run(["git", "prune"])

    print("Running git gc...")
    run(["git", "gc"])

    print("Done.")


if __name__ == "__main__":
    main()
