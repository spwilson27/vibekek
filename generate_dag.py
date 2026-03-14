#!/usr/bin/env python3
"""
Generate DAG JSON from task markdown files.
Extracts task IDs and dependencies from markdown headers and depends_on fields.
"""

import json
import os
import re
from pathlib import Path

def extract_task_id(filepath: str, base_dir: str) -> str:
    """Extract relative task ID from filepath (relative to base_dir)."""
    rel_path = os.path.relpath(filepath, base_dir)
    return rel_path

def parse_depends_on(content: str) -> list:
    """Parse depends_on field from task markdown."""
    # Look for depends_on: [...] pattern
    match = re.search(r'depends_on:\s*\[(.*?)\]', content, re.DOTALL)
    if not match:
        return []
    
    deps_str = match.group(1).strip()
    if not deps_str:
        return []
    
    # Parse dependencies - they can be on same line or multiple lines
    deps = []
    # Split by comma or newline
    for dep in re.split(r'[,\n]', deps_str):
        dep = dep.strip()
        if dep and dep != 'none':
            # Remove quotes if present
            dep = dep.strip('"\'')
            if dep:
                deps.append(dep)
    
    return deps

def normalize_dependency(dep: str, task_sub_epic_dir: str, phase_dir: str) -> str:
    """
    Normalize a dependency path.
    
    The DAG format expects:
    - For same-phase dependencies: relative path within the phase (e.g., `05_risk_001_verification/01_risk_matrix_extraction.md`)
    - For cross-phase dependencies: relative path from tasks directory (e.g., `phase_1/03_template_resolution_context/01_template_resolver_skeleton.md`)
    
    Args:
        dep: The dependency path from the task file (may be full path or relative)
        task_sub_epic_dir: The sub-epic directory of the task (e.g., `05_risk_001_verification`)
        phase_dir: The phase directory name (e.g., `phase_5`)
    
    Returns:
        Normalized dependency path suitable for the DAG JSON
    """
    # If it's a full path from project root (docs/plan/tasks/phase_X/...), make it relative
    if dep.startswith('docs/plan/tasks/'):
        # Remove the docs/plan/tasks/ prefix
        rel_to_tasks = dep.replace('docs/plan/tasks/', '')
        
        # Check if it's a same-phase dependency
        if rel_to_tasks.startswith(phase_dir + '/'):
            # Remove the phase_X/ prefix for same-phase dependencies
            return rel_to_tasks.replace(phase_dir + '/', '')
        else:
            # Cross-phase dependency - keep the phase_X/ prefix
            return rel_to_tasks
    
    # If dep doesn't have a path separator, it's in the same sub-epic directory
    if '/' not in dep:
        # Add .md extension if missing
        if not dep.endswith('.md'):
            dep = dep + '.md'
        return f"{task_sub_epic_dir}/{dep}"
    
    # Already has a path - might be sub_epic/file.md format (same-phase)
    # or phase_X/sub_epic/file.md format (cross-phase)
    if not dep.endswith('.md'):
        dep = dep + '.md'
    
    # Check if it starts with a phase directory (cross-phase)
    if re.match(r'^phase_\d+/', dep):
        return dep
    
    # Otherwise it's a same-phase dependency
    return dep

def main():
    base_dir = '/home/mrwilson/software/devs/docs/plan/tasks/phase_5'
    phase_dir = 'phase_5'
    
    # Find all .md files (excluding README.md and other non-task files)
    task_files = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.endswith('.md') and f not in ['README.md', 'SUB_EPIC_SUMMARY.md']:
                filepath = os.path.join(root, f)
                task_files.append(filepath)
    
    # Build DAG
    dag = {}
    
    for filepath in task_files:
        task_id = extract_task_id(filepath, base_dir)
        task_sub_epic_dir = os.path.dirname(task_id)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        deps = parse_depends_on(content)
        
        # Convert dependency names to full task IDs
        full_deps = []
        for dep in deps:
            full_dep = normalize_dependency(dep, task_sub_epic_dir, phase_dir)
            full_deps.append(full_dep)
        
        dag[task_id] = full_deps
    
    # Write DAG JSON
    output_path = os.path.join(base_dir, 'dag.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(dag, f, indent=2, sort_keys=True)
    
    print(f"Generated DAG with {len(dag)} tasks")
    print(f"Output: {output_path}")

if __name__ == '__main__':
    main()
