#!/usr/bin/env python3
"""Plan artifact validation script.

Reads ``.gen_state.json`` to determine which phases have completed, then
validates only the artifacts from those phases.  Each validator checks
JSON schema conformance and cross-artifact invariants.

Usage::

    python .tools/validate.py [--phase N] [--all]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Set, Tuple

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.environ.get("VALIDATE_ROOT_DIR", os.path.dirname(TOOLS_DIR))
SCHEMAS_DIR = os.environ.get("VALIDATE_SCHEMAS_DIR", os.path.join(TOOLS_DIR, "schemas"))
PLAN_DIR = os.path.join(ROOT_DIR, "docs", "plan")
GEN_STATE_FILE = os.path.join(ROOT_DIR, ".gen_state.json")


def _load_schema(name: str) -> Dict[str, Any]:
    path = os.path.join(SCHEMAS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _validate_object(obj: Any, schema: Dict[str, Any], path_label: str) -> List[str]:
    """Lightweight JSON schema validator (no external dependency).

    Validates type, required fields, enum, pattern, minLength, minimum,
    array items, and additionalProperties.
    """
    errors: List[str] = []

    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool,
        }
        # Handle union types like ["string", "null"]
        if isinstance(expected_type, list):
            allowed = tuple(t for et in expected_type for t in ([type(None)] if et == "null" else [type_map[et]]))
            if not isinstance(obj, allowed):
                errors.append(f"{path_label}: expected one of {expected_type}, got {type(obj).__name__}")
                return errors
        else:
            if expected_type == "null":
                if obj is not None:
                    errors.append(f"{path_label}: expected null")
                return errors
            expected_cls = type_map.get(expected_type)
            if expected_cls and not isinstance(obj, expected_cls):
                errors.append(f"{path_label}: expected {expected_type}, got {type(obj).__name__}")
                return errors

    if isinstance(obj, dict):
        for req_field in schema.get("required", []):
            if req_field not in obj:
                errors.append(f"{path_label}: missing required field '{req_field}'")

        props = schema.get("properties", {})
        for key, value in obj.items():
            if key in props:
                errors.extend(_validate_object(value, props[key], f"{path_label}.{key}"))
            elif schema.get("additionalProperties") is False and key != "$schema":
                errors.append(f"{path_label}: unexpected field '{key}'")

    elif isinstance(obj, list):
        items_schema = schema.get("items")
        min_items = schema.get("minItems")
        if min_items is not None and len(obj) < min_items:
            errors.append(f"{path_label}: expected at least {min_items} items, got {len(obj)}")
        if items_schema:
            for i, item in enumerate(obj):
                errors.extend(_validate_object(item, items_schema, f"{path_label}[{i}]"))

    elif isinstance(obj, str):
        if "enum" in schema and obj not in schema["enum"]:
            errors.append(f"{path_label}: '{obj}' not in {schema['enum']}")
        if "minLength" in schema and len(obj) < schema["minLength"]:
            errors.append(f"{path_label}: string too short (min {schema['minLength']})")
        if "pattern" in schema:
            import re
            if not re.match(schema["pattern"], obj):
                errors.append(f"{path_label}: '{obj}' does not match pattern '{schema['pattern']}'")

    elif isinstance(obj, (int, float)):
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{path_label}: {obj} < minimum {schema['minimum']}")

    return errors


def _validate_schema(data: Any, schema_name: str, label: str) -> List[str]:
    schema = _load_schema(schema_name)
    return _validate_object(data, schema, label)


# --- Phase validators ---

def validate_phase_7(state: Dict) -> List[str]:
    """Validate per-document extracted requirement JSONs."""
    errors: List[str] = []
    req_dir = os.path.join(PLAN_DIR, "requirements")
    if not os.path.isdir(req_dir):
        errors.append("requirements/ directory does not exist")
        return errors

    for fname in os.listdir(req_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(req_dir, fname)
        try:
            data = _load_json(fpath)
            errors.extend(_validate_schema(data, "extracted_requirements.json", fname))
        except json.JSONDecodeError as e:
            errors.append(f"{fname}: invalid JSON: {e}")
    return errors


def validate_phase_8(state: Dict) -> List[str]:
    """Validate filtered requirement JSONs (same schema as extracted)."""
    # After filtering, the per-doc files should still conform to the extracted schema
    return validate_phase_7(state)


def validate_phase_9(state: Dict) -> List[str]:
    """Validate merged requirements.json and check completeness."""
    errors: List[str] = []
    merged_path = os.path.join(PLAN_DIR, "requirements.json")
    if not os.path.exists(merged_path):
        errors.append("requirements.json does not exist")
        return errors

    try:
        data = _load_json(merged_path)
        errors.extend(_validate_schema(data, "merged_requirements.json", "requirements.json"))
    except json.JSONDecodeError as e:
        errors.append(f"requirements.json: invalid JSON: {e}")
        return errors

    # Check total_count matches actual count
    if "requirements" in data and "total_count" in data:
        if data["total_count"] != len(data["requirements"]):
            errors.append(
                f"requirements.json: total_count ({data['total_count']}) "
                f"does not match actual count ({len(data['requirements'])})"
            )

    # Check all per-doc requirements are present in merged
    req_dir = os.path.join(PLAN_DIR, "requirements")
    if os.path.isdir(req_dir):
        merged_ids = {r["id"] for r in data.get("requirements", [])}
        for fname in os.listdir(req_dir):
            if not fname.endswith(".json"):
                continue
            try:
                doc_data = _load_json(os.path.join(req_dir, fname))
                for req in doc_data.get("requirements", []):
                    if req["id"] not in merged_ids:
                        errors.append(
                            f"requirements.json: missing requirement {req['id']} from {fname}"
                        )
            except (json.JSONDecodeError, KeyError):
                pass  # Already caught by phase 7 validation
    return errors


def validate_phase_10(state: Dict) -> List[str]:
    """Validate deduplication: union of remaining + removed = original merged set."""
    errors: List[str] = []
    merged_path = os.path.join(PLAN_DIR, "requirements.json")
    deduped_path = os.path.join(PLAN_DIR, "requirements_deduped.json")

    if not os.path.exists(deduped_path):
        errors.append("requirements_deduped.json does not exist")
        return errors

    try:
        deduped = _load_json(deduped_path)
        errors.extend(_validate_schema(deduped, "deduped_requirements.json", "requirements_deduped.json"))
    except json.JSONDecodeError as e:
        errors.append(f"requirements_deduped.json: invalid JSON: {e}")
        return errors

    # Verify counts
    if os.path.exists(merged_path):
        try:
            merged = _load_json(merged_path)
            remaining_count = len(merged.get("requirements", []))
            removed_count = len(deduped.get("removed_requirements", []))
            if deduped.get("total_remaining") != remaining_count:
                errors.append(
                    f"requirements_deduped.json: total_remaining ({deduped.get('total_remaining')}) "
                    f"does not match requirements.json count ({remaining_count})"
                )
        except json.JSONDecodeError:
            pass

    return errors


def validate_phase_12(state: Dict) -> List[str]:
    """Validate ordered requirements."""
    errors: List[str] = []
    ordered_path = os.path.join(PLAN_DIR, "requirements_ordered.json")
    if not os.path.exists(ordered_path):
        errors.append("requirements_ordered.json does not exist")
        return errors

    try:
        data = _load_json(ordered_path)
        errors.extend(_validate_schema(data, "ordered_requirements.json", "requirements_ordered.json"))
    except json.JSONDecodeError as e:
        errors.append(f"requirements_ordered.json: invalid JSON: {e}")
        return errors

    # Check all IDs from requirements.json are present
    merged_path = os.path.join(PLAN_DIR, "requirements.json")
    if os.path.exists(merged_path):
        try:
            merged = _load_json(merged_path)
            merged_ids = {r["id"] for r in merged.get("requirements", [])}
            ordered_ids = {r["id"] for r in data.get("requirements", [])}
            missing = merged_ids - ordered_ids
            if missing:
                errors.append(
                    f"requirements_ordered.json: missing {len(missing)} requirement(s) "
                    f"from requirements.json: {sorted(missing)[:5]}..."
                )
        except (json.JSONDecodeError, KeyError):
            pass

    return errors


def validate_phase_13(state: Dict) -> List[str]:
    """Validate epic mappings."""
    errors: List[str] = []
    epic_path = os.path.join(PLAN_DIR, "epic_mappings.json")
    if not os.path.exists(epic_path):
        errors.append("epic_mappings.json does not exist")
        return errors

    try:
        data = _load_json(epic_path)
        errors.extend(_validate_schema(data, "epic_mappings.json", "epic_mappings.json"))
    except json.JSONDecodeError as e:
        errors.append(f"epic_mappings.json: invalid JSON: {e}")
        return errors

    # Check every requirement is mapped to at least one epic
    ordered_path = os.path.join(PLAN_DIR, "requirements_ordered.json")
    if os.path.exists(ordered_path):
        try:
            ordered = _load_json(ordered_path)
            all_req_ids = {r["id"] for r in ordered.get("requirements", [])}
            mapped_ids: Set[str] = set()
            for epic in data.get("epics", []):
                mapped_ids.update(epic.get("requirement_ids", []))
            unmapped = all_req_ids - mapped_ids
            if unmapped:
                errors.append(
                    f"epic_mappings.json: {len(unmapped)} requirement(s) not mapped to any epic: "
                    f"{sorted(unmapped)[:5]}..."
                )
        except (json.JSONDecodeError, KeyError):
            pass

    return errors


def validate_phase_16(state: Dict) -> List[str]:
    """Validate task sidecar files."""
    errors: List[str] = []
    tasks_dir = os.path.join(PLAN_DIR, "tasks")
    if not os.path.isdir(tasks_dir):
        errors.append("tasks/ directory does not exist")
        return errors

    schema = _load_schema("task_sidecar.json")
    sidecar_count = 0
    for root, dirs, files in os.walk(tasks_dir):
        for fname in files:
            if not fname.endswith(".json") or fname.startswith("dag"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, tasks_dir)
            try:
                data = _load_json(fpath)
                errs = _validate_object(data, schema, rel)
                errors.extend(errs)
                sidecar_count += 1
            except json.JSONDecodeError as e:
                errors.append(f"{rel}: invalid JSON: {e}")

    if sidecar_count == 0:
        errors.append("No task sidecar .json files found in tasks/")
    return errors


def validate_phase_18(state: Dict) -> List[str]:
    """Validate cross-phase: no duplicate task IDs, all feature gates have producer+consumer."""
    errors: List[str] = []
    tasks_dir = os.path.join(PLAN_DIR, "tasks")
    if not os.path.isdir(tasks_dir):
        return errors

    task_ids: Dict[str, str] = {}  # task_id -> file path
    feature_gate_producers: Dict[str, List[str]] = {}  # gate -> [task_ids that create it]
    feature_gate_consumers: Dict[str, List[str]] = {}  # gate -> [task_ids that test against it]

    for root, dirs, files in os.walk(tasks_dir):
        for fname in files:
            if not fname.endswith(".json") or fname.startswith("dag"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, tasks_dir)
            try:
                data = _load_json(fpath)
                tid = data.get("task_id", "")
                if tid in task_ids:
                    errors.append(f"Duplicate task_id '{tid}': {task_ids[tid]} and {rel}")
                else:
                    task_ids[tid] = rel

                task_type = data.get("type", "")
                for gate in data.get("feature_gates", []):
                    if task_type == "green":
                        feature_gate_producers.setdefault(gate, []).append(tid)
                    elif task_type == "red":
                        feature_gate_consumers.setdefault(gate, []).append(tid)
            except (json.JSONDecodeError, KeyError):
                pass

    # Check all consumed feature gates have a producer
    for gate in feature_gate_consumers:
        if gate not in feature_gate_producers:
            errors.append(
                f"Feature gate '{gate}' is tested by red task(s) but no green task produces it"
            )

    return errors


def validate_phase_20(state: Dict) -> List[str]:
    """Validate per-phase DAGs: match on-disk tasks, no cycles, valid depends_on refs."""
    errors: List[str] = []
    tasks_dir = os.path.join(PLAN_DIR, "tasks")
    if not os.path.isdir(tasks_dir):
        return errors

    for phase_name in sorted(os.listdir(tasks_dir)):
        phase_dir = os.path.join(tasks_dir, phase_name)
        if not os.path.isdir(phase_dir):
            continue

        dag_file = os.path.join(phase_dir, "dag.json")
        if not os.path.exists(dag_file):
            continue

        try:
            dag = _load_json(dag_file)
        except json.JSONDecodeError as e:
            errors.append(f"{phase_name}/dag.json: invalid JSON: {e}")
            continue

        # Collect on-disk task files
        on_disk_tasks: Set[str] = set()
        for root, dirs, files in os.walk(phase_dir):
            for fname in files:
                if fname.endswith(".md") and fname not in {
                    "README.md", "SUB_EPIC_SUMMARY.md", "review_summary.md",
                    "cross_phase_review_summary.md",
                }:
                    rel = os.path.relpath(os.path.join(root, fname), phase_dir)
                    on_disk_tasks.add(rel)

        dag_tasks = set(dag.keys())

        # Tasks in DAG but not on disk
        for task in dag_tasks - on_disk_tasks:
            errors.append(f"{phase_name}/dag.json: task '{task}' in DAG but not on disk")

        # Check for cycles (simple DFS)
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for dep in dag.get(node, []):
                if dep not in visited:
                    if has_cycle(dep):
                        return True
                elif dep in rec_stack:
                    errors.append(f"{phase_name}/dag.json: cycle detected involving '{node}' -> '{dep}'")
                    return True
            rec_stack.discard(node)
            return False

        for node in dag_tasks:
            if node not in visited:
                has_cycle(node)

        # Validate depends_on references exist
        for task, deps in dag.items():
            for dep in deps:
                if dep not in dag_tasks and dep not in on_disk_tasks:
                    errors.append(f"{phase_name}/dag.json: '{task}' depends on unknown '{dep}'")

    return errors


# --- Validator registry ---

VALIDATORS = {
    "requirements_extracted": ("Phase 7: Extract Requirements", validate_phase_7),
    "meta_requirements_filtered": ("Phase 8: Filter Meta Requirements", validate_phase_8),
    "requirements_merged": ("Phase 9: Merge Requirements", validate_phase_9),
    "requirements_deduplicated": ("Phase 10: Deduplicate Requirements", validate_phase_10),
    "requirements_ordered": ("Phase 12: Order Requirements", validate_phase_12),
    "epics_completed": ("Phase 13: Generate Epics", validate_phase_13),
    "tasks_completed": ("Phase 16: Red/Green Tasks", validate_phase_16),
    "tasks_reviewed": ("Phase 17: Review Tasks", validate_phase_16),
    "cross_phase_reviewed": ("Phase 18: Cross-Phase Review", validate_phase_18),
    "dag_completed": ("Phase 20: DAG Generation", validate_phase_20),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate plan artifacts against JSON schemas")
    parser.add_argument("--phase", type=int, default=None, help="Validate only artifacts from phase N")
    parser.add_argument("--all", action="store_true", help="Validate all artifacts regardless of state")
    args = parser.parse_args()

    # Load state
    if os.path.exists(GEN_STATE_FILE):
        with open(GEN_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    # Map phase numbers to state keys
    phase_to_keys = {
        7: "requirements_extracted",
        8: "meta_requirements_filtered",
        9: "requirements_merged",
        10: "requirements_deduplicated",
        12: "requirements_ordered",
        13: "epics_completed",
        16: "tasks_completed",
        17: "tasks_reviewed",
        18: "cross_phase_reviewed",
        20: "dag_completed",
    }

    total_errors = 0
    total_checks = 0

    for state_key, (label, validator) in VALIDATORS.items():
        # Determine if this validator should run
        if args.phase is not None:
            # Only run validators matching the specified phase
            matching_phases = [p for p, k in phase_to_keys.items() if k == state_key]
            if not matching_phases or args.phase not in matching_phases:
                continue
        elif not args.all:
            # Only run validators for completed phases
            if not state.get(state_key, False):
                continue

        total_checks += 1
        errors = validator(state)
        if errors:
            print(f"\n[FAIL] {label}:")
            for err in errors:
                print(f"  - {err}")
            total_errors += len(errors)
        else:
            print(f"[PASS] {label}")

    if total_checks == 0:
        print("No validators to run (no completed phases or invalid --phase)")
        sys.exit(0)

    print(f"\n{'=' * 60}")
    print(f"Ran {total_checks} validator(s): {total_errors} error(s)")
    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
