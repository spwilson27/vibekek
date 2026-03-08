import argparse
import os
import re
import sys
import json

# Regex to match requirements like [REQ-123], [TAS-001], [REQ-SEC-001], [1_PRD-REQ-001]
REQ_REGEX = re.compile(r"\[([A-Z0-9_]+-[A-Z0-9\-_]+)\]")

def parse_requirements(file_path):
    """Extracts all requirement IDs from a given file."""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return set()
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    return set(REQ_REGEX.findall(content))

def verify_doc(source_file, extracted_file):
    """Verifies that all requirements from source_file exist in extracted_file."""
    print(f"Verifying {source_file} against {extracted_file}...")
    source_reqs = parse_requirements(source_file)
    extracted_reqs = parse_requirements(extracted_file)
    
    missing_in_extracted = source_reqs - extracted_reqs
    missing_in_source = extracted_reqs - source_reqs
    
    success = True
    
    if missing_in_extracted:
        print(f"FAILED: The following {len(missing_in_extracted)} requirements are missing from {extracted_file}:")
        for req in sorted(missing_in_extracted):
            print(f"  - [{req}]")
        success = False
        
    if missing_in_source:
        print(f"FAILED: The following {len(missing_in_source)} requirements are missing from {source_file}:")
        for req in sorted(missing_in_source):
            print(f"  - [{req}]")
        success = False
        
    if success:
        print(f"Success: Both {source_file} and {extracted_file} perfectly match exactly the same {len(source_reqs)} requirement IDs.")
        return 0
    else:
        return 1

def verify_master(master_file, requirements_dir):
    """Verifies that all requirements from requirements_dir exist in the master_file."""
    print(f"Verifying {requirements_dir} against {master_file}...")
    
    all_extracted_reqs = set()
    if not os.path.exists(requirements_dir) or not os.path.isdir(requirements_dir):
        print(f"Error: Directory not found: {requirements_dir}")
        return 1

    for filename in os.listdir(requirements_dir):
        if filename.endswith(".md"):
            file_path = os.path.join(requirements_dir, filename)
            reqs = parse_requirements(file_path)
            all_extracted_reqs.update(reqs)
            
    master_reqs = parse_requirements(master_file)
    
    missing = all_extracted_reqs - master_reqs
    
    if not missing:
        print(f"Success: All {len(all_extracted_reqs)} extracted requirements are present in the master list ({master_file}).")
        return 0
    else:
        print(f"FAILED: The following {len(missing)} requirements are missing from the master list ({master_file}):")
        print("They must be either copied over fully or explicitly listed in the 'Removed or Modified Requirements' section.")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1

def verify_phases(master_file, phases_dir):
    """Verifies that all requirements from the master requirements list exist in the phases directory."""
    print(f"Verifying {phases_dir} covers all requirements in {master_file}...")
    
    master_reqs = parse_requirements(master_file)
    phases_reqs = set()
    
    if not os.path.exists(phases_dir) or not os.path.isdir(phases_dir):
        print(f"Error: Directory not found or not a directory: {phases_dir}")
        return 1

    for filename in os.listdir(phases_dir):
        if filename.endswith(".md"):
            file_path = os.path.join(phases_dir, filename)
            phases_reqs.update(parse_requirements(file_path))
    
    missing = master_reqs - phases_reqs
    
    if not missing:
        print(f"Success: All {len(master_reqs)} requirements from {master_file} are mapped to a phase in {phases_dir}.")
        return 0
    else:
        print(f"FAILED: The following {len(missing)} requirements are NOT mapped to any phase in {phases_dir}:")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1

def verify_tasks(phases_dir, tasks_dir):
    """Verifies that all requirements mapped in the phases directory exist in the tasks directory."""
    print(f"Verifying {tasks_dir} covers all requirements mapped in {phases_dir}...")
    
    phases_reqs = set()
    if not os.path.exists(phases_dir) or not os.path.isdir(phases_dir):
        print(f"Error: Directory not found or not a directory: {phases_dir}")
        return 1

    for filename in os.listdir(phases_dir):
        if filename.endswith(".md"):
            file_path = os.path.join(phases_dir, filename)
            phases_reqs.update(parse_requirements(file_path))
            
    tasks_reqs = set()
    if not os.path.exists(tasks_dir) or not os.path.isdir(tasks_dir):
        print(f"Error: Directory not found or not a directory: {tasks_dir}")
        return 1

    for root, _, files in os.walk(tasks_dir):
        for filename in files:
            if filename.endswith(".md"):
                file_path = os.path.join(root, filename)
                tasks_reqs.update(parse_requirements(file_path))
        
    missing = phases_reqs - tasks_reqs
    
    if not missing:
        print(f"Success: All {len(phases_reqs)} requirements from {phases_dir} are mapped to a task in {tasks_dir}.")
        return 0
    else:
        print(f"FAILED: The following {len(missing)} requirements are NOT mapped to any task in {tasks_dir}:")
        for req in sorted(missing):
            print(f"  - [{req}]")
        return 1

def verify_ordered(master_file, ordered_file):
    """Verifies that all ACTIVE requirements from the master list exist in the ordered document."""
    print(f"Verifying {ordered_file} covers all active requirements in {master_file}...")
    
    with open(master_file, 'r', encoding='utf-8') as f:
        master_content = f.read()
        
    # Split to find active vs removed
    parts = re.split(r'(?i)#+\s*Removed or Modified Requirements', master_content)
    active_content = parts[0]
    
    active_reqs = set(REQ_REGEX.findall(active_content))
    ordered_reqs = parse_requirements(ordered_file)
    
    missing = active_reqs - ordered_reqs
    extra = ordered_reqs - active_reqs
    
    success = True
    if missing:
        print(f"FAILED: The following {len(missing)} active requirements are missing from {ordered_file}:")
        for req in sorted(missing):
            print(f"  - [{req}]")
        success = False
        
    if extra:
        print(f"FAILED: The following {len(extra)} requirements in {ordered_file} are invalid or were supposed to be removed:")
        for req in sorted(extra):
            print(f"  - [{req}]")
        success = False
        
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
        print(f"FAILED: Found invalid dependencies in {ordered_file}. Dependencies must be requirement tags (e.g. [REQ-123]) or 'None':")
        for line_num, dep, line in invalid_deps:
            print(f"  Line {line_num}: Invalid format '{dep}' in '{line}'")
        success = False
        
    if missing_deps:
        print(f"FAILED: Found dependencies in {ordered_file} that do not exist in active requirements of {master_file}:")
        for line_num, dep, line in missing_deps:
            print(f"  Line {line_num}: Missing requirement {dep} in '{line}'")
        success = False

    if success:
        print(f"Success: {ordered_file} contains exactly the {len(active_reqs)} active requirements from {master_file}, and all dependencies are valid.")
        return 0
    else:
        return 1

def verify_json_grouping(phase_file, json_file):
    """Verifies that the JSON grouping file perfectly matches the requirements in the phase file."""
    print(f"Verifying {json_file} groups all requirements in {phase_file}...")
    
    phase_reqs = parse_requirements(phase_file)
    
    if not os.path.exists(json_file):
        print(f"Error: JSON file not found: {json_file}")
        return 1
        
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            groupings = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format in {json_file}: {e}")
        return 1
        
    json_reqs = set()
    for _, req_list in groupings.items():
        if isinstance(req_list, list):
            json_reqs.update(req_list)
            
    missing = phase_reqs - json_reqs
    extra = json_reqs - phase_reqs
    
    success = True
    if missing:
        print(f"FAILED: The grouping JSON missed the following {len(missing)} requirements from {phase_file}:")
        for req in sorted(missing):
            print(f"  - [{req}]")
        success = False
        
    if extra:
        print(f"FAILED: The grouping JSON added the following {len(extra)} hallucinated requirements not in {phase_file}:")
        for req in sorted(extra):
            print(f"  - [{req}]")
        success = False
        
    if success:
        print(f"Success: The JSON mapping accurately groups exactly the {len(phase_reqs)} requirements from {phase_file}.")
        return 0
    else:
        return 1

def verify_dags(tasks_dir):
    """Verifies all dag.json or dag_reviewed.json files in the tasks directory."""
    print(f"Verifying all DAG files in {tasks_dir}...")
    
    # 1. Load the master DAG exactly like run_workflow.py does
    master_dag = {}
    phase_dirs = []
    if not os.path.exists(tasks_dir):
        print(f"Error: Directory not found: {tasks_dir}")
        return 1

    all_success = True
    for phase_dir in sorted(os.listdir(tasks_dir)):
        phase_path = os.path.join(tasks_dir, phase_dir)
        if not os.path.isdir(phase_path) or not phase_dir.startswith("phase_"):
            continue
        
        phase_dirs.append(phase_dir)
        
        # Use dag_reviewed.json if it exists, otherwise dag.json
        dag_file = os.path.join(phase_path, "dag_reviewed.json")
        if not os.path.exists(dag_file):
            dag_file = os.path.join(phase_path, "dag.json")
            
        if os.path.exists(dag_file):
            print(f"  Checking {dag_file}...")
            try:
                with open(dag_file, "r", encoding="utf-8") as f:
                    phase_dag = json.load(f)
                    
                # Check for orphans and file existence WITHIN this phase
                if not _verify_phase_consistency(phase_path, phase_dag):
                    all_success = False

                # Check for cycles within this phase's DAG
                phase_cycle = _find_cycle(phase_dag)
                if phase_cycle:
                    print(f"    FAILED: Cycle detected in {phase_dir} DAG: {' -> '.join(phase_cycle)}")
                    all_success = False

                for task_id, prerequisites in phase_dag.items():
                    full_task_id = f"{phase_dir}/{task_id}"
                    master_dag[full_task_id] = [f"{phase_dir}/{p}" for p in prerequisites]
            except Exception as e:
                print(f"    FAILED: Error processing {dag_file}: {e}")
                all_success = False
    
    if not all_success:
        return 1

    # 2. Check the master DAG for cycles
    print("  Checking for cycles in combined Master DAG...")
    cycle = _find_cycle(master_dag)
    if cycle:
        print(f"    FAILED: Cycle detected in Master DAG: {' -> '.join(cycle)}")
        return 1
    
    print(f"Success: All DAGs are valid, consistent with files on disk, and cycle-free across all {len(phase_dirs)} phases.")
    return 0

def _verify_phase_consistency(phase_path, phase_dag):
    """Checks if all tasks in the DAG exist on disk and if there are orphan tasks."""
    # Every key and every dependency should exist as a file or directory
    all_referenced_tasks = set(phase_dag.keys())
    for deps in phase_dag.values():
        all_referenced_tasks.update(deps)
        
    missing_tasks = []
    for task_id in all_referenced_tasks:
        task_full_path = os.path.join(phase_path, task_id)
        if not os.path.exists(task_full_path):
            missing_tasks.append(task_id)
            
    if missing_tasks:
        print(f"    FAILED: The following tasks referenced in DAG do not exist on disk:")
        for t in sorted(missing_tasks):
            print(f"      - {t}")
        return False
        
    # Check for orphans: markdown files on disk not mentioned in the DAG
    # Exclude non-task files (review summaries, reorder reports, etc.)
    _NON_TASK_PATTERNS = {"review_summary.md", "cross_phase_review_summary", "reorder_tasks_summary"}
    orphan_tasks = []
    for root, dirs, files in os.walk(phase_path):
        for file in files:
            if file.endswith(".md"):
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, phase_path)

                # Skip known non-task markdown files
                if any(pat in rel_p for pat in _NON_TASK_PATTERNS):
                    continue
                
                # If the exact file is not a key, check if its parent directory is a key
                # some tasks are represented as directories in the DAG
                if rel_p not in phase_dag:
                    # Check parent directories
                    parent = os.path.dirname(rel_p)
                    found_parent = False
                    while parent:
                        if parent in phase_dag:
                            found_parent = True
                            break
                        new_parent = os.path.dirname(parent)
                        if new_parent == parent: break
                        parent = new_parent
                    
                    if not found_parent:
                        orphan_tasks.append(rel_p)
                    
    if orphan_tasks:
        print(f"    FAILED: The following {len(orphan_tasks)} .md files are not tracked in the DAG:")
        for t in sorted(orphan_tasks[:10]):
            print(f"      - {t}")
        if len(orphan_tasks) > 10:
            print(f"      ... and {len(orphan_tasks)-10} more.")
        return False

    return True

def _find_cycle(dag):
    """Returns a list representing a cycle if found, else None."""
    visited = set()
    stack = []
    stack_set = set()
    
    def dfs(node):
        if node in stack_set:
            # Found a cycle!
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

def verify_req_format(file_path):
    """Verifies that all requirement IDs in a file follow the standard format [DOC_PREFIX-REQ-NNN]."""
    print(f"Verifying requirement ID format in {file_path}...")

    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return 1

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    all_ids = set(REQ_REGEX.findall(content))
    # Standard format: DOC_PREFIX-REQ-NNN or DOC_PREFIX-REQ-CATEGORY-NNN
    # e.g. 1_PRD-REQ-001, 5_SECURITY_DESIGN-REQ-BR-SEC-TM-001
    standard_format = re.compile(r'^[A-Z0-9_]+-REQ-(?:[A-Z0-9]+-)*\d{3,}$')

    non_standard = []
    for req_id in sorted(all_ids):
        if not standard_format.match(req_id):
            non_standard.append(req_id)

    if non_standard:
        print(f"FAILED: The following {len(non_standard)} requirement IDs do not follow the standard format [DOC_PREFIX-REQ-NNN]:")
        for req_id in non_standard:
            print(f"  - [{req_id}] (expected format: [DOC_PREFIX-REQ-001])")
        return 1

    print(f"Success: All {len(all_ids)} requirement IDs in {file_path} follow the standard format.")
    return 0


def verify_uniqueness(directory):
    """Verifies that no requirement ID appears in more than one document within a directory."""
    print(f"Verifying requirement ID uniqueness across {directory}...")

    if not os.path.exists(directory) or not os.path.isdir(directory):
        print(f"Error: Directory not found: {directory}")
        return 1

    id_to_files = {}
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
        print(f"Success: All {len(id_to_files)} requirement IDs are unique across {directory}.")
        return 0

    print(f"FAILED: {len(duplicates)} requirement IDs appear in multiple files:")
    for req_id in sorted(duplicates):
        files = ", ".join(sorted(duplicates[req_id]))
        print(f"  - [{req_id}] in: {files}")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Verify requirement extraction consistency.")
    parser.add_argument("--verify-doc", nargs=2, metavar=("SOURCE_FILE", "EXTRACTED_FILE"),
                        help="Verify that all requirements in SOURCE_FILE are present in EXTRACTED_FILE")
    parser.add_argument("--verify-master", action="store_true",
                        help="Verify that all requirements from the requirements/ directory are in the master requirements.md")
    parser.add_argument("--verify-phases", nargs=2, metavar=("MASTER_FILE", "PHASES_DIR"),
                        help="Verify that all requirements in MASTER_FILE are mapped within PHASES_DIR")
    parser.add_argument("--verify-ordered", nargs=2, metavar=("MASTER_FILE", "ORDERED_FILE"),
                        help="Verify that all ACTIVE requirements in MASTER_FILE are mapped within ORDERED_FILE")
    parser.add_argument("--verify-json", nargs=2, metavar=("PHASE_FILE", "JSON_FILE"),
                        help="Verify that the JSON_FILE sub-epic mappings perfectly match the PHASE_FILE requirements")
    parser.add_argument("--verify-tasks", nargs=2, metavar=("PHASES_DIR", "TASKS_DIR"),
                        help="Verify that all requirements in PHASES_DIR are mapped within TASKS_DIR")
    parser.add_argument("--verify-dags", metavar="TASKS_DIR",
                        help="Verify that all dag.json files in TASKS_DIR are traversable and consistent")
    parser.add_argument("--verify-req-format", metavar="FILE",
                        help="Verify that all requirement IDs in FILE follow the standard format [DOC_PREFIX-REQ-NNN]")
    parser.add_argument("--verify-uniqueness", metavar="DIR",
                        help="Verify that no requirement ID appears in multiple files within DIR")

    args = parser.parse_args()
    
    exit_code = 0
    if args.verify_doc:
        source_file, extracted_file = args.verify_doc
        exit_code = verify_doc(source_file, extracted_file)
        
    elif args.verify_master:
        # Default paths relative to project root
        master_file = "requirements.md"
        requirements_dir = "docs/plan/requirements"
        
        # Check if we are inside scripts/ folder and adjust paths if needed
        if os.path.basename(os.getcwd()) == "scripts":
            master_file = "../requirements.md"
            requirements_dir = "../docs/plan/requirements"
            
        exit_code = verify_master(master_file, requirements_dir)
        
    elif args.verify_phases:
        master_file, phases_dir = args.verify_phases
        exit_code = verify_phases(master_file, phases_dir)
        
    elif args.verify_ordered:
        master_file, ordered_file = args.verify_ordered
        exit_code = verify_ordered(master_file, ordered_file)
        
    elif args.verify_json:
        phase_file, json_file = args.verify_json
        exit_code = verify_json_grouping(phase_file, json_file)
        
    elif args.verify_tasks:
        phases_dir, tasks_dir = args.verify_tasks
        exit_code = verify_tasks(phases_dir, tasks_dir)
        
    elif args.verify_dags:
        tasks_dir = args.verify_dags
        exit_code = verify_dags(tasks_dir)

    elif args.verify_req_format:
        exit_code = verify_req_format(args.verify_req_format)

    elif args.verify_uniqueness:
        exit_code = verify_uniqueness(args.verify_uniqueness)

    else:
        parser.print_help()
        sys.exit(1)
        
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
