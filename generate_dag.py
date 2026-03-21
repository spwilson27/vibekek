#!/usr/bin/env python3
"""Generate DAG JSON files for a phase by parsing task markdown files."""

import json
import os
import re
import sys
from pathlib import Path


def parse_depends_on(content: str, current_group: str) -> list[str]:
    """Extract depends_on list from markdown content and resolve to full paths."""
    # Look for depends_on in the content (usually in a Dependencies section)
    match = re.search(r'depends_on:\s*\[(.*?)\]', content, re.DOTALL)
    if not match:
        return []
    
    deps_str = match.group(1).strip()
    if deps_str.lower() == 'none' or not deps_str:
        return []
    
    # Parse the list items
    deps = []
    for item in re.findall(r'["\']?([^"\',\]]+)["\']?', deps_str):
        item = item.strip()
        if not item or item.lower() == 'none':
            continue
        # If the dependency doesn't contain a '/', it's just a filename in the same group
        if '/' not in item:
            item = f"{current_group}/{item}"
        deps.append(item)
    
    return deps


def find_task_files(phase_dir: Path) -> list[tuple[str, Path]]:
    """Find all .md task files in the phase directory, grouped by directory."""
    task_files = []
    for root, dirs, files in os.walk(phase_dir):
        # Skip the phase directory itself
        if Path(root) == phase_dir:
            continue
        for file in files:
            if file.endswith('.md'):
                task_file = Path(root) / file
                # Get the group directory name
                group = Path(root).relative_to(phase_dir).as_posix()
                task_files.append((group, task_file))
    return sorted(task_files, key=lambda x: (x[0], x[1].name))


def generate_dag(phase_dir: Path) -> dict:
    """Generate DAG dictionary for a phase."""
    dag = {}
    
    task_files = find_task_files(phase_dir)
    
    for group, task_file in task_files:
        # Get relative path from phase_dir
        rel_path = task_file.relative_to(phase_dir).as_posix()
        
        # Read and parse the file
        content = task_file.read_text(encoding='utf-8')
        depends_on = parse_depends_on(content, group)
        
        # Store in DAG
        dag[rel_path] = depends_on
    
    return dag


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <phase_dir>")
        sys.exit(1)
    
    phase_dir = Path(sys.argv[1])
    
    if not phase_dir.exists():
        print(f"Error: Phase directory does not exist: {phase_dir}")
        sys.exit(1)
    
    dag = generate_dag(phase_dir)
    
    # Output JSON
    output_file = phase_dir / 'dag.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(dag, f, indent=2, sort_keys=True)
    
    print(f"Generated {output_file} with {len(dag)} tasks")


if __name__ == '__main__':
    main()
