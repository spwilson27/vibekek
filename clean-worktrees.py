#!/usr/bin/env python3

import subprocess
import sys


def run(args, **kwargs):
    return subprocess.run(args, **kwargs)


def check(args):
    return subprocess.run(args, capture_output=True)


def main():
    result = check(["git", "rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0:
        print("Error: Not a git repository.")
        sys.exit(1)

    print("Removing all secondary git worktrees...")

    result = check(["git", "worktree", "list"])
    lines = result.stdout.decode().splitlines()
    worktree_paths = [line.split()[0] for line in lines[1:] if line.strip()]

    for path in worktree_paths:
        print(f"Removing worktree: {path}")
        run(["git", "worktree", "remove", "--force", path])

    print("Running git worktree prune...")
    run(["git", "worktree", "prune"])

    print("Deleting branches starting with 'ai-phase'...")
    result = check(["git", "branch", "--format=%(refname:short)", "--list", "ai-phase*"])
    branches = [b for b in result.stdout.decode().splitlines() if b.strip()]
    for branch in branches:
        print(f"Deleting branch: {branch}")
        run(["git", "branch", "-D", branch])

    print("Running prune...")
    run(["git", "prune"])

    print("Running git gc...")
    run(["git", "gc"])

    print("Done.")


if __name__ == "__main__":
    main()
