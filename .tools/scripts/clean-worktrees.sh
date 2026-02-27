#!/bin/bash

# Ensure we are in a git repository
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "Error: Not a git repository."
    exit 1
fi

echo "Removing all secondary git worktrees..."

# Get the list of all worktrees except the main one
# `git worktree list` first line is always the main worktree
git worktree list | awk '{print $1}' | tail -n +2 | while read -r worktree_path; do
    # Skip if the path is empty
    if [ -z "$worktree_path" ]; then
        continue
    fi
    
    echo "Removing worktree: $worktree_path"
    git worktree remove --force "$worktree_path"
done

echo "Running git worktree prune..."
git worktree prune

echo "Deleting branches starting with 'ai-phase'..."
for branch in $(git branch --format='%(refname:short)' --list 'ai-phase*'); do
    echo "Deleting branch: $branch"
    git branch -D "$branch"
done

echo "Running prune"
git prune

echo "Running git gc..."
git gc

echo "Done."

