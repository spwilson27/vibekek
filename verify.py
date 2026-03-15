#!/usr/bin/env python3
"""
Unified verification script for the devs planning workflow.

This script consolidates all verification checks into a single command-line tool
with subcommands for specific verification types.

Usage:
    python .tools/verify.py <subcommand> [options]

Subcommands:
    all                 Run all verification checks
    req-format          Verify requirement ID format
    req-uniqueness      Verify requirement ID uniqueness
    req-desc-length     Verify requirement description length
    doc                 Verify document extraction consistency
    master              Verify master requirements completeness
    phases              Verify phase mapping coverage
    tasks               Verify task mapping coverage
    ordered             Verify ordered requirements
    json                Verify JSON grouping files
    dags                Verify DAG files
    depends-on          Verify depends_on metadata format

Examples:
    python .tools/verify.py all
    python .tools/verify.py dags docs/plan/tasks/
    python .tools/verify.py depends-on docs/plan/tasks/phase_1/
    python .tools/verify.py req-format requirements.md
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ANSI color codes for terminal output
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Regex to match requirements like [REQ-123], [TAS-001], [REQ-SEC-001], etc.
REQ_REGEX = re.compile(r"\[([A-Z0-9_]+-[A-Z0-9\._-]+)\]")

# Files to exclude from DAG validation
_NON_TASK_FILES = {
    "README.md",
    "SUB_EPIC_SUMMARY.md",
    "REQUIREMENTS_TRACEABILITY.md",
    "REQUIREMENTS_COVERAGE_MAP.md",
    "REQUIREMENTS_COVERAGE.md",
    "REQUIREMENTS_MATRIX.md",
    "IMPLEMENTATION_SUMMARY.md",
    "review_summary.md",
    "cross_phase_review_summary.md",
    "cross_phase_review_summary_pass_1.md",
    "cross_phase_review_summary_pass_2.md",
    "reorder_tasks_summary.md",
    "reorder_tasks_summary_pass_1.md",
    "reorder_tasks_summary_pass_2.md",
    "00_index.md",
}


# ============================================================================
# Requirement Parsing Utilities
# ============================================================================

def parse_requirements(file_path: str) -> Set[str]:
    """Extracts all requirement IDs from a given file."""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return set()

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    return set(REQ_REGEX.findall(content))


# Regex to match requirement *definitions* (heading format: ### **[ID]** ...)
_REQ_DEFINITION_REGEX = re.compile(r"^###\s+\*\*\[([A-Z0-9_]+-[A-Z0-9\._-]+)\]\*\*", re.MULTILINE)


def parse_requirement_definitions(file_path: str) -> Set[str]:
    """Extracts only *defined* requirement IDs (heading declarations) from a file.

    Unlike :func:`parse_requirements` which captures every bracketed ID
    (including inline prose references like acceptance-criteria sub-IDs,
    risk IDs, and template placeholders), this function only returns IDs
    that appear as formal requirement headings (``### **[ID]** ...``).
    """
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return set()

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    return set(_REQ_DEFINITION_REGEX.findall(content))


# ============================================================================
# Requirement Verification Functions
# ============================================================================

def verify_req_format(file_path: str) -> int:
    """Verifies that all requirement IDs in a file follow the standard format."""
    print(f"Verifying requirement ID format in {file_path}...")

    if not os.path.exists(file_path):
        print(f"{RED}Error: File not found: {file_path}{RESET}")
        return 1

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    all_ids = set(REQ_REGEX.findall(content))
    # Standard format: alphanumeric segments separated by hyphens
    standard_format = re.compile(r'^[A-Z0-9][A-Z0-9_]*(?:-[A-Z0-9\._][A-Z0-9\._-]*)*$')

    non_standard = []
    for req_id in sorted(all_ids):
        if not standard_format.match(req_id):
            non_standard.append(req_id)

    if non_standard:
        print(f"{RED}FAILED: The following {len(non_standard)} requirement IDs do not follow the standard format:{RESET}")
        for req_id in non_standard:
            print(f"  - [{req_id}] (expected format: [DOC_PREFIX-REQ-001])")
        return 1

    print(f"{GREEN}Success: All {len(all_ids)} requirement IDs follow the standard format.{RESET}")
    return 0


def verify_req_uniqueness(directory: str) -> int:
    """Verifies that no requirement ID appears in more than one document."""
    print(f"Verifying requirement ID uniqueness across {directory}...")

    if not os.path.exists(directory) or not os.path.isdir(directory):
        print(f"{RED}Error: Directory not found: {directory}{RESET}")
        return 1

    id_to_files: Dict[str, List[str]] = {}
    for root, _, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".md"):
                file_path = os.path.join(root, filename)
                reqs = parse_requirements(file_path)
                rel_path = os.path.relpath(file_path, directory)
                for req_id in reqs:
                    id_to_files.setdefault(req_id, []).append(rel_path)

    duplicates = {k: v for k, v in id_to_files.items() if len(v) > 1}

    if not duplicates:
        print(f"{GREEN}Success: All {len(id_to_files)} requirement IDs are unique.{RESET}")
        return 0

    print(f"{RED}FAILED: {len(duplicates)} requirement IDs appear in multiple files:{RESET}")
    for req_id in sorted(duplicates):
        files_list = sorted(duplicates[req_id])
        files_str = ", ".join(files_list)
        print(f"  - [{req_id}] in: {files_str}")
    return 1


def verify_req_desc_length(file_path: str, min_words: int = 10) -> int:
    """Verifies that all requirements have descriptions of at least min_words words."""
    print(f"Verifying requirement descriptions have at least {min_words} words...")

    if not os.path.exists(file_path):
        print(f"{RED}Error: File not found: {file_path}{RESET}")
        return 1

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    req_block_pattern = re.compile(
        r'### \*\*\[([A-Z0-9_]+-[A-Z0-9\._-]+)\]\*\*.*?(?=### \*\*\[|\Z)',
        re.DOTALL
    )
    desc_pattern = re.compile(r'\*\*Description:\*\*\s*(.+?)(?=\n- \*\*|\Z)', re.DOTALL)

    all_reqs = set(REQ_REGEX.findall(content))
    short_descriptions: List[Tuple[str, int, str]] = []

    for match in req_block_pattern.finditer(content):
        req_id = match.group(1)
        block = match.group(0)

        desc_match = desc_pattern.search(block)
        if desc_match:
            description = desc_match.group(1).strip()
            words = description.split()
            word_count = len(words)

            if word_count < min_words:
                short_descriptions.append((req_id, word_count, description[:100] + "..." if len(description) > 100 else description))

    if short_descriptions:
        print(f"{RED}FAILED: {len(short_descriptions)} requirements have descriptions with fewer than {min_words} words:{RESET}")
        for req_id, word_count, desc_preview in sorted(short_descriptions):
            print(f"  - [{req_id}] ({word_count} words): {desc_preview}")
        return 1

    print(f"{GREEN}Success: All {len(all_reqs)} requirements have descriptions with at least {min_words} words.{RESET}")
    return 0


# ============================================================================
# Document Verification Functions
# ============================================================================

def verify_doc(source_file: str, extracted_file: str) -> int:
    """Verifies that all requirements from source_file exist in extracted_file."""
    print(f"Verifying {source_file} against {extracted_file}...")
    source_reqs = parse_requirements(source_file)
    extracted_reqs = parse_requirements(extracted_file)

    missing_in_extracted = source_reqs - extracted_reqs
    missing_in_source = extracted_reqs - source_reqs

    success = True

    if missing_in_extracted:
        print(f"{RED}FAILED: {len(missing_in_extracted)} requirements missing from {extracted_file}:{RESET}")
        for req in sorted(missing_in_extracted):
            print(f"  - [{req}]")
        success = False

    if missing_in_source:
        print(f"{RED}FAILED: {len(missing_in_source)} requirements missing from {source_file}:{RESET}")
        for req in sorted(missing_in_source):
            print(f"  - [{req}]")
        success = False

    if success:
        print(f"{GREEN}Success: Both files match exactly with {len(source_reqs)} requirement IDs.{RESET}")
        return 0
    return 1


def verify_master(master_file: str, requirements_dir: str) -> int:
    """Verifies that all requirements from requirements_dir exist in the master_file.

    Only *defined* requirements (heading declarations ``### **[ID]** ...``)
    from the extracted docs are checked, not inline prose references.
    """
    print(f"Verifying {requirements_dir} against {master_file}...")

    all_extracted_reqs = set()
    if not os.path.exists(requirements_dir) or not os.path.isdir(requirements_dir):
        print(f"{RED}Error: Directory not found: {requirements_dir}{RESET}")
        return 1

    for filename in os.listdir(requirements_dir):
        if filename.endswith(".md"):
            file_path = os.path.join(requirements_dir, filename)
            reqs = parse_requirement_definitions(file_path)
            all_extracted_reqs.update(reqs)

    master_reqs = parse_requirements(master_file)
    missing = all_extracted_reqs - master_reqs

    if not missing:
        print(f"{GREEN}Success: All {len(all_extracted_reqs)} extracted requirements are in the master list.{RESET}")
        return 0
    else:
        print(f"{RED}FAILED: {len(missing)} requirements missing from the master list:{RESET}")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1


def verify_phases(master_file: str, phases_dir: str) -> int:
    """Verifies that all requirements from master exist in the phases directory."""
    print(f"Verifying {phases_dir} covers all requirements in {master_file}...")

    master_reqs = parse_requirements(master_file)
    phases_reqs = set()

    if not os.path.exists(phases_dir) or not os.path.isdir(phases_dir):
        print(f"{RED}Error: Directory not found: {phases_dir}{RESET}")
        return 1

    for filename in os.listdir(phases_dir):
        if filename.endswith(".md"):
            file_path = os.path.join(phases_dir, filename)
            phases_reqs.update(parse_requirements(file_path))

    missing = master_reqs - phases_reqs

    if not missing:
        print(f"{GREEN}Success: All {len(master_reqs)} requirements are mapped to phases.{RESET}")
        return 0
    else:
        print(f"{RED}FAILED: {len(missing)} requirements NOT mapped to any phase:{RESET}")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1


def verify_tasks(phases_dir: str, tasks_dir: str) -> int:
    """Verifies that all requirements mapped in phases exist in tasks."""
    print(f"Verifying {tasks_dir} covers all requirements mapped in {phases_dir}...")

    phases_reqs = set()
    if not os.path.exists(phases_dir) or not os.path.isdir(phases_dir):
        print(f"{RED}Error: Directory not found: {phases_dir}{RESET}")
        return 1

    for filename in os.listdir(phases_dir):
        if filename.endswith(".md") and filename != "phase_removed.md":
            file_path = os.path.join(phases_dir, filename)
            phases_reqs.update(parse_requirements(file_path))

    tasks_reqs = set()
    if not os.path.exists(tasks_dir) or not os.path.isdir(tasks_dir):
        print(f"{RED}Error: Directory not found: {tasks_dir}{RESET}")
        return 1

    for root, _, files in os.walk(tasks_dir):
        for filename in files:
            if filename.endswith(".md"):
                file_path = os.path.join(root, filename)
                tasks_reqs.update(parse_requirements(file_path))

    missing = phases_reqs - tasks_reqs

    if not missing:
        print(f"{GREEN}Success: All {len(phases_reqs)} requirements are mapped to tasks.{RESET}")
        return 0
    else:
        print(f"{RED}FAILED: {len(missing)} requirements NOT mapped to any task:{RESET}")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1


def verify_ordered(master_file: str, ordered_file: str) -> int:
    """Verifies that all ACTIVE requirements from master exist in ordered document."""
    print(f"Verifying {ordered_file} covers all active requirements in {master_file}...")

    with open(master_file, 'r', encoding='utf-8') as f:
        master_content = f.read()

    parts = re.split(r'(?i)#+\s*Removed or Modified Requirements', master_content)
    active_content = parts[0]
    active_reqs = set(REQ_REGEX.findall(active_content))
    ordered_reqs = parse_requirements(ordered_file)

    missing = active_reqs - ordered_reqs
    extra = ordered_reqs - active_reqs

    success = True
    if missing:
        print(f"{RED}FAILED: {len(missing)} active requirements missing from {ordered_file}:{RESET}")
        for req in sorted(missing):
            print(f"  - [{req}]")
        success = False

    if extra:
        print(f"{RED}FAILED: {len(extra)} invalid/removed requirements in {ordered_file}:{RESET}")
        for req in sorted(extra):
            print(f"  - [{req}]")
        success = False

    # Validate dependency format
    invalid_deps = []
    missing_deps = []

    with open(ordered_file, 'r', encoding='utf-8') as f:
        ordered_content_lines = f.read().splitlines()

    for i, line in enumerate(ordered_content_lines, 1):
        if line.strip().startswith("- **Dependencies:**"):
            parts = line.split("**Dependencies:**")
            if len(parts) < 2:
                continue
            deps_str = parts[1].strip()
            if deps_str.lower() in ("none", "", "n/a"):
                continue

            deps = [d.strip() for d in deps_str.split(",")]
            for dep in deps:
                match = re.match(r"^\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]$", dep)
                if not match:
                    invalid_deps.append((i, dep, line.strip()))
                else:
                    req_id = match.group(1)
                    if req_id not in active_reqs:
                        missing_deps.append((i, dep, line.strip()))

    if invalid_deps:
        print(f"{RED}FAILED: Invalid dependency format in {ordered_file}:{RESET}")
        for line_num, dep, line in invalid_deps:
            print(f"  Line {line_num}: Invalid format '{dep}' in '{line}'")
        success = False

    if missing_deps:
        print(f"{RED}FAILED: Dependencies reference missing requirements:{RESET}")
        for line_num, dep, line in missing_deps:
            print(f"  Line {line_num}: Missing requirement {dep} in '{line}'")
        success = False

    if success:
        print(f"{GREEN}Success: {ordered_file} contains exactly {len(active_reqs)} active requirements.{RESET}")
        return 0
    return 1


def verify_json_grouping(phase_file: str, json_file: str) -> int:
    """Verifies that the JSON grouping file matches the requirements in the phase file."""
    print(f"Verifying {json_file} groups all requirements in {phase_file}...")

    phase_reqs = parse_requirements(phase_file)

    if not os.path.exists(json_file):
        print(f"{RED}Error: JSON file not found: {json_file}{RESET}")
        return 1

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            groupings = json.load(f)
    except json.JSONDecodeError as e:
        print(f"{RED}Error: Invalid JSON format in {json_file}: {e}{RESET}")
        return 1

    json_reqs = set()
    for _, req_list in groupings.items():
        if isinstance(req_list, list):
            json_reqs.update(req_list)

    missing = phase_reqs - json_reqs
    extra = json_reqs - phase_reqs

    success = True
    if missing:
        print(f"{RED}FAILED: JSON missed {len(missing)} requirements from {phase_file}:{RESET}")
        for req in sorted(missing):
            print(f"  - [{req}]")
        success = False

    if extra:
        print(f"{RED}FAILED: JSON has {len(extra)} hallucinated requirements:{RESET}")
        for req in sorted(extra):
            print(f"  - [{req}]")
        success = False

    if success:
        print(f"{GREEN}Success: JSON mapping accurately groups {len(phase_reqs)} requirements.{RESET}")
        return 0
    return 1


# ============================================================================
# DAG Verification Functions
# ============================================================================

def _find_cycle(dag: Dict[str, List[str]]) -> Optional[List[str]]:
    """Returns a list representing a cycle if found, else None."""
    visited: Set[str] = set()
    stack: List[str] = []
    stack_set: Set[str] = set()

    def dfs(node: str) -> Optional[List[str]]:
        if node in stack_set:
            try:
                idx = stack.index(node)
                return stack[idx:] + [node]
            except ValueError:
                return [node, "???", node]
        if node in visited:
            return None

        visited.add(node)
        stack.append(node)
        stack_set.add(node)

        for neighbor in dag.get(node, []):
            res = dfs(neighbor)
            if res:
                return res

        stack.pop()
        stack_set.remove(node)
        return None

    for node in dag:
        if node not in visited:
            cycle = dfs(node)
            if cycle:
                return cycle
    return None


def _verify_phase_consistency(phase_path: str, phase_dag: Dict[str, List[str]]) -> bool:
    """Checks if all tasks in the DAG exist on disk and if there are orphan tasks."""
    all_referenced_tasks = set(phase_dag.keys())
    for deps in phase_dag.values():
        all_referenced_tasks.update(deps)

    missing_tasks = []
    for task_id in all_referenced_tasks:
        task_full_path = os.path.join(phase_path, task_id)
        if not os.path.exists(task_full_path):
            missing_tasks.append(task_id)

    if missing_tasks:
        print(f"    {RED}FAILED: Tasks referenced in DAG do not exist on disk:{RESET}")
        for t in sorted(missing_tasks):
            print(f"      - {t}")
        return False

    # Check for orphans
    orphan_tasks = []
    for root, dirs, files in os.walk(phase_path):
        for file in files:
            if file.endswith(".md"):
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, phase_path)

                if any(pat in rel_p for pat in _NON_TASK_FILES):
                    continue

                if rel_p not in phase_dag:
                    parent = os.path.dirname(rel_p)
                    found_parent = False
                    while parent:
                        if parent in phase_dag:
                            found_parent = True
                            break
                        new_parent = os.path.dirname(parent)
                        if new_parent == parent:
                            break
                        parent = new_parent

                    if not found_parent:
                        orphan_tasks.append(rel_p)

    if orphan_tasks:
        print(f"    {RED}FAILED: {len(orphan_tasks)} .md files not tracked in DAG:{RESET}")
        for t in sorted(orphan_tasks[:10]):
            print(f"      - {t}")
        if len(orphan_tasks) > 10:
            print(f"      ... and {len(orphan_tasks) - 10} more.")
        return False

    return True


def verify_dags(tasks_dir: str) -> int:
    """Verifies all DAG files in the tasks directory."""
    print(f"Verifying all DAG files in {tasks_dir}...")

    if not os.path.exists(tasks_dir):
        print(f"{RED}Error: Directory not found: {tasks_dir}{RESET}")
        return 1

    master_dag: Dict[str, List[str]] = {}
    phase_dirs = []
    all_success = True

    for phase_dir in sorted(os.listdir(tasks_dir)):
        phase_path = os.path.join(tasks_dir, phase_dir)
        if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
            continue

        phase_dirs.append(phase_dir)

        dag_file = os.path.join(phase_path, "dag_reviewed.json")
        if not os.path.exists(dag_file):
            dag_file = os.path.join(phase_path, "dag.json")

        if os.path.exists(dag_file):
            print(f"  Checking {dag_file}...")
            try:
                with open(dag_file, "r", encoding='utf-8') as f:
                    phase_dag = json.load(f)

                if not _verify_phase_consistency(phase_path, phase_dag):
                    all_success = False

                phase_cycle = _find_cycle(phase_dag)
                if phase_cycle:
                    print(f"    {RED}FAILED: Cycle detected in {phase_dir} DAG: {' -> '.join(phase_cycle)}{RESET}")
                    all_success = False

                for task_id, prerequisites in phase_dag.items():
                    full_task_id = f"{phase_dir}/{task_id}"
                    master_dag[full_task_id] = [f"{phase_dir}/{p}" for p in prerequisites]
            except Exception as e:
                print(f"    {RED}FAILED: Error processing {dag_file}: {e}{RESET}")
                all_success = False
        else:
            has_tasks = any(
                os.path.isdir(os.path.join(phase_path, d))
                for d in os.listdir(phase_path)
            )
            if has_tasks:
                print(f"  {RED}FAILED: {phase_dir} has no DAG file.{RESET}")
                print(f"    Run 'workflow.py plan --phase 7-dag --force' to regenerate.")
                all_success = False

    if not all_success:
        return 1

    print("  Checking for cycles in combined Master DAG...")
    cycle = _find_cycle(master_dag)
    if cycle:
        print(f"    {RED}FAILED: Cycle detected in Master DAG: {' -> '.join(cycle)}{RESET}")
        return 1

    print(f"{GREEN}Success: All DAGs are valid across {len(phase_dirs)} phases.{RESET}")
    return 0


# ============================================================================
# Depends-On Verification Functions
# ============================================================================

def verify_depends_on(tasks_dir: str, auto_fix: bool = False) -> int:
    """Verifies depends_on metadata format in task files.
    
    This is a simplified inline implementation that checks for:
    - Missing depends_on metadata fields
    - Relative paths with ../ prefixes
    - Full absolute paths (docs/plan/tasks/...)
    
    For full validation with detailed error reporting and auto-fix capabilities,
    agents should fix issues manually based on the error output.
    """
    tasks_path = Path(tasks_dir).resolve()
    
    if not tasks_path.exists():
        print(f"{RED}Error: Directory not found: {tasks_path}{RESET}")
        return 1
    
    print(f"{BOLD}Validating depends_on metadata in: {tasks_path}{RESET}\n")
    
    errors = []
    warnings = []
    
    # Collect all task files
    task_files = []
    for phase_dir in tasks_path.iterdir():
        if not phase_dir.is_dir() or not phase_dir.name.startswith('phase_'):
            continue
        for sub_epic_dir in phase_dir.iterdir():
            if not sub_epic_dir.is_dir():
                continue
            for md_file in sub_epic_dir.iterdir():
                if md_file.suffix == ".md" and md_file.name not in _NON_TASK_FILES:
                    task_files.append(md_file)
    
    print(f"Found {len(task_files)} task files to validate\n")
    
    for task_file in task_files:
        try:
            content = task_file.read_text(encoding="utf-8")
        except OSError:
            continue
        
        task_id = str(task_file.relative_to(tasks_path.parent))
        
        # Check for depends_on metadata
        has_depends_on = False
        lines = content.split('\n')[:50]
        for line in lines:
            if re.search(r'-\s*depends_on:', line, re.IGNORECASE):
                has_depends_on = True
                
                # Check for problematic patterns
                if '../' in line:
                    errors.append({
                        "task_id": task_id,
                        "message": f"Relative path with ../ detected in: {line.strip()}",
                        "suggestion": "Use paths relative to tasks/ directory (e.g., phase_X/sub_epic/file.md)"
                    })
                
                if 'docs/plan/tasks/' in line:
                    warnings.append({
                        "task_id": task_id,
                        "message": f"Full absolute path detected: {line.strip()}",
                        "suggestion": "Use relative paths for better portability"
                    })
                break
        
        if not has_depends_on:
            errors.append({
                "task_id": task_id,
                "message": "Missing depends_on metadata field",
                "suggestion": "Add: - depends_on: [01_prerequisite_task.md]"
            })
    
    # Report results
    if errors:
        print(f"\n{RED}{BOLD}=== ERRORS ({len(errors)}) ==={RESET}")
        for err in errors:
            print(f"\n{RED}ERROR{RESET}: {err['task_id']}")
            print(f"  → {err['message']}")
            if err.get('suggestion'):
                print(f"  💡 Suggestion: {err['suggestion']}")
    
    if warnings:
        print(f"\n{YELLOW}{BOLD}=== WARNINGS ({len(warnings)}) ==={RESET}")
        for warn in warnings:
            print(f"\n{YELLOW}WARNING{RESET}: {warn['task_id']}")
            print(f"  → {warn['message']}")
    
    print(f"\n{BOLD}Summary:{RESET}")
    print(f"  Errors:   {RED}{len(errors)}{RESET}")
    print(f"  Warnings: {YELLOW}{len(warnings)}{RESET}")
    
    if errors:
        print(f"\n{RED}✗ Validation FAILED{RESET}")
        if auto_fix:
            print(f"\nRun with --fix flag to attempt automatic fixes")
        return 1
    else:
        print(f"\n{GREEN}✓ Validation PASSED{RESET}")
        return 0


# ============================================================================
# "All" Verification
# ============================================================================

def verify_all(root_dir: str) -> int:
    """Run all verification checks."""
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}Running all verification checks{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    results: Dict[str, bool] = {}
    req_file = os.path.join(root_dir, "requirements.md")
    phases_dir = os.path.join(root_dir, "docs", "plan", "phases")
    tasks_dir = os.path.join(root_dir, "docs", "plan", "tasks")
    requirements_dir = os.path.join(root_dir, "docs", "plan", "requirements")

    # Requirement checks
    if os.path.exists(req_file):
        print(f"\n{BOLD}[1/8] Requirement Format{RESET}")
        results["req-format"] = verify_req_format(req_file) == 0

        print(f"\n{BOLD}[2/8] Requirement Description Length{RESET}")
        results["req-desc-length"] = verify_req_desc_length(req_file) == 0

        print(f"\n{BOLD}[3/8] Master Requirements{RESET}")
        results["master"] = verify_master(req_file, requirements_dir) == 0

        print(f"\n{BOLD}[4/8] Phase Mapping{RESET}")
        results["phases"] = verify_phases(req_file, phases_dir) == 0

        print(f"\n{BOLD}[5/8] Task Mapping{RESET}")
        results["tasks"] = verify_tasks(phases_dir, tasks_dir) == 0

        print(f"\n{BOLD}[6/8] DAG Validation{RESET}")
        results["dags"] = verify_dags(tasks_dir) == 0

        print(f"\n{BOLD}[7/8] Depends-On Metadata{RESET}")
        results["depends-on"] = verify_depends_on(tasks_dir) == 0

    # Ordered requirements (if exists)
    ordered_file = os.path.join(root_dir, "ordered_requirements.md")
    if os.path.exists(ordered_file):
        print(f"\n{BOLD}[8/8] Ordered Requirements{RESET}")
        results["ordered"] = verify_ordered(req_file, ordered_file) == 0

    # Summary
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}Verification Summary{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for check, result in sorted(results.items()):
        status = f"{GREEN}PASS{RESET}" if result else f"{RED}FAIL{RESET}"
        print(f"  {status}  {check}")

    print(f"\nTotal: {passed}/{total} checks passed")

    if passed == total:
        print(f"\n{GREEN}✓ All verification checks passed!{RESET}")
        return 0
    else:
        print(f"\n{RED}✗ Some verification checks failed.{RESET}")
        return 1


# ============================================================================
# CLI Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Unified verification script for the devs planning workflow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s all
      Run all verification checks

  %(prog)s req-format requirements.md
      Verify requirement ID format

  %(prog)s dags docs/plan/tasks/
      Verify all DAG files

  %(prog)s depends-on docs/plan/tasks/
      Verify depends_on metadata format

  %(prog)s depends-on --fix docs/plan/tasks/
      Verify and auto-fix depends_on formatting issues
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Verification subcommand")

    # All verification
    subparsers.add_parser("all", help="Run all verification checks")

    # Requirement checks
    p_req_format = subparsers.add_parser("req-format", help="Verify requirement ID format")
    p_req_format.add_argument("file", help="Requirements file to verify")

    p_req_unique = subparsers.add_parser("req-uniqueness", help="Verify requirement ID uniqueness")
    p_req_unique.add_argument("dir", help="Directory to check")

    p_req_desc = subparsers.add_parser("req-desc-length", help="Verify requirement description length")
    p_req_desc.add_argument("file", help="Requirements file to verify")
    p_req_desc.add_argument("--min-words", type=int, default=10, help="Minimum word count (default: 10)")

    # Document checks
    p_doc = subparsers.add_parser("doc", help="Verify document extraction consistency")
    p_doc.add_argument("source_file", help="Source document")
    p_doc.add_argument("extracted_file", help="Extracted requirements file")

    p_master = subparsers.add_parser("master", help="Verify master requirements completeness")
    p_master.add_argument("master_file", help="Master requirements file")
    p_master.add_argument("requirements_dir", help="Extracted requirements directory")

    p_phases = subparsers.add_parser("phases", help="Verify phase mapping coverage")
    p_phases.add_argument("master_file", help="Master requirements file")
    p_phases.add_argument("phases_dir", help="Phases directory")

    p_tasks = subparsers.add_parser("tasks", help="Verify task mapping coverage")
    p_tasks.add_argument("phases_dir", help="Phases directory")
    p_tasks.add_argument("tasks_dir", help="Tasks directory")

    p_ordered = subparsers.add_parser("ordered", help="Verify ordered requirements")
    p_ordered.add_argument("master_file", help="Master requirements file")
    p_ordered.add_argument("ordered_file", help="Ordered requirements file")

    p_json = subparsers.add_parser("json", help="Verify JSON grouping files")
    p_json.add_argument("phase_file", help="Phase requirements file")
    p_json.add_argument("json_file", help="JSON grouping file")

    # DAG checks
    p_dags = subparsers.add_parser("dags", help="Verify DAG files")
    p_dags.add_argument("tasks_dir", help="Tasks directory")

    # Depends-on checks
    p_depends = subparsers.add_parser("depends-on", help="Verify depends_on metadata format")
    p_depends.add_argument("tasks_dir", help="Tasks directory")
    p_depends.add_argument("--fix", action="store_true", help="Auto-fix formatting issues")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    exit_code = 0

    if args.command == "all":
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        exit_code = verify_all(root_dir)

    elif args.command == "req-format":
        exit_code = verify_req_format(args.file)

    elif args.command == "req-uniqueness":
        exit_code = verify_req_uniqueness(args.dir)

    elif args.command == "req-desc-length":
        exit_code = verify_req_desc_length(args.file, args.min_words)

    elif args.command == "doc":
        exit_code = verify_doc(args.source_file, args.extracted_file)

    elif args.command == "master":
        exit_code = verify_master(args.master_file, args.requirements_dir)

    elif args.command == "phases":
        exit_code = verify_phases(args.master_file, args.phases_dir)

    elif args.command == "tasks":
        exit_code = verify_tasks(args.phases_dir, args.tasks_dir)

    elif args.command == "ordered":
        exit_code = verify_ordered(args.master_file, args.ordered_file)

    elif args.command == "json":
        exit_code = verify_json_grouping(args.phase_file, args.json_file)

    elif args.command == "dags":
        exit_code = verify_dags(args.tasks_dir)

    elif args.command == "depends-on":
        exit_code = verify_depends_on(args.tasks_dir, auto_fix=args.fix)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
