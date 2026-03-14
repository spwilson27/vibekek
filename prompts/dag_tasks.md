# Goal

You are an expert Technical Program Manager. Your task is to analyze a set of technical tasks for a given project phase and construct a Dependency Graph (DAG) representing the required execution order.

# Input

**Phase:** {phase_filename}
**Target Output File:** {target_path}

## Project Context
<context>
{description_ctx}
</context>

## Tasks in this Phase
<tasks>
{tasks_content}
</tasks>

# Instructions

1.  **Extract Task IDs:** Each task in the `<tasks>` section is marked with a header like `### Task ID: sub_epic_dir/task_file.md`. You MUST use the **full relative path** as the task ID, including:
    *   The sub-epic directory name (e.g., `01_mcp_tool_reliability_recovery`)
    *   A forward slash `/`
    *   The task markdown filename with `.md` extension (e.g., `01_agent_search_before_read_policy.md`)

    **Correct format:** `sub_epic_dir/task_file.md`
    **Example:** `01_mcp_tool_reliability_recovery/01_agent_search_before_read_policy.md`

    **WRONG examples (do NOT use these formats):**
    - `01_agent_search_before_read_policy` (missing sub-epic dir and .md extension)
    - `01_mcp_tool_reliability_recovery` (just the sub-epic directory, missing task file)
    - `phase_4/01_mcp_tool_reliability_recovery/01_agent_search_before_read_policy.md` (don't include phase directory)

    **DO NOT include these files in the DAG:**
    - `README.md` files (sub-epic documentation, not tasks)
    - `SUB_EPIC_SUMMARY.md` files (organizational summaries, not tasks)
    - `review_summary.md` files (review artifacts, not tasks)

2.  **Analyze Dependencies:** Carefully read through each task provided in the `<tasks>` section. Identify strict logical dependencies:
    *   **Data/State Dependency:** Does Task B require the database schema created by Task A?
    *   **Interface Dependency:** Does Task B call an API endpoint or use a class defined in Task A?
    *   **Setup Dependency:** Does Task B require configuration or infrastructure provisioned by Task A?
3.  **Avoid Circular Dependencies:** Ensure that your dependencies form a Directed Acyclic Graph (DAG). Task A cannot depend on Task B if Task B depends on Task A.
4.  **Optimize for Parallelism:** Only add a dependency if it is *strictly* necessary. If two components (e.g., a frontend UI component and a deep backend service) can be developed independently based on an agreed-upon interface in another task, do not make them depend on each other. They should only depend on the task that defines the interface. This allows multiple agents to work on tasks concurrently.
5.  **Format:** Your output must be ONLY a valid JSON object.
    *   The keys of the JSON object must be the precise task IDs extracted from the "Task ID" headers (format: `sub_epic_dir/task_file.md`).
    *   The value for each key must be a JSON array of strings, where each string is the precise task ID of a task that MUST be completed BEFORE the keyed task can begin.
    *   If a task has no prerequisites, its value should be an empty array `[]`.
    *   **CRITICAL:** Every task file on disk must have a corresponding key in the JSON. Do not miss any tasks.

# Output Format

Your final response MUST be enclosed within a json codeblock. No other text.

# CONSTRAINTS
- You MUST use your file editing tools to write the output directly into the provided `Target Output File` path. End your turn immediately once the file is written.
- Every task ID in your JSON must exactly match the "Task ID" header from the input, including the `.md` extension.

```json
{
  "01_mcp_tool_reliability_recovery/01_agent_search_before_read_policy.md": [],
  "01_mcp_tool_reliability_recovery/02_agent_stage_output_truncation_logic.md": ["01_mcp_tool_reliability_recovery/01_agent_search_before_read_policy.md"],
  "01_mcp_tool_reliability_recovery/03_agent_bulk_status_monitoring_protocol.md": ["01_mcp_tool_reliability_recovery/01_agent_search_before_read_policy.md"],
  "01_mcp_tool_reliability_recovery/04_agent_session_recovery_order.md": ["01_mcp_tool_reliability_recovery/02_agent_stage_output_truncation_logic.md", "01_mcp_tool_reliability_recovery/03_agent_bulk_status_monitoring_protocol.md"]
}
```

<thinking>
1. Extract all task IDs from the "Task ID: sub_epic_dir/task_file.md" headers in the input.
2. Re-read all tasks carefully.
3. For each task, ask: "What MUST exist before this can be started safely by an independent agent?"
4. Map these dependencies using the exact task IDs (format: sub_epic_dir/task_file.md).
5. Double check for circular dependencies.
6. Verify every task on disk has a corresponding key in the JSON.
7. Create the JSON output.
</thinking>

# VERIFICATION STEPS (REQUIRED - DO NOT SKIP)

After writing the DAG JSON file, you MUST verify your work is complete by running these checks:

## Step 1: Validate JSON syntax
```bash
python -c "import json; json.load(open('{target_path}'))"
```
If this fails, fix the JSON syntax error.

## Step 2: Validate depends_on metadata format
Before verifying the DAG, ensure all task files have properly formatted `depends_on` metadata:

```bash
python .tools/verify.py depends-on docs/plan/tasks/{phase_filename}/
```

**This validation checks:**
- Every task file has a `depends_on` metadata field
- Paths are in a consistent, parseable format
- No problematic patterns (multi-line arrays, inconsistent quoting, etc.)

**If validation fails:**
1. Read each error carefully
2. Fix the `depends_on` lines in the task files
3. Re-run validation until it passes

**Common issues and fixes:**

| Issue | Bad Example | Good Example |
|-------|-------------|--------------|
| Missing metadata | (no depends_on line) | `- depends_on: [01_prerequisite.md]` |
| Relative paths | `../other_epic/01_file.md` | `other_epic/01_file.md` |
| Full paths | `docs/plan/tasks/phase_X/...` | `phase_X/sub_epic/file.md` |
| Inconsistent quotes | `[file1.md, "file2.md"]` | `["file1.md", "file2.md"]` |
| Multi-line array | `depends_on: [\n  file1.md\n]` | `depends_on: [file1.md, file2.md]` |

**Auto-fix:** You can also run `python .tools/verify.py depends-on --fix docs/plan/tasks/` to automatically fix formatting issues.

## Step 3: Run the DAG verification script
```bash
python .tools/verify.py dags docs/plan/tasks/
```

## Step 4: Check for these specific errors and fix them:

| Error | What it means | How to fix |
|-------|---------------|------------|
| `DAG references non-existent file: X` | Your JSON has a key or dependency that doesn't exist on disk | Remove the invalid reference or correct the task ID to match the actual file path |
| `File on disk not in DAG: X` | A task `.md` file exists but is not in your JSON | Add the missing task to your JSON with its dependencies (or `[]` if no deps). **NOTE:** Ignore `README.md`, `SUB_EPIC_SUMMARY.md`, and `review_summary.md` files - these are documentation, not tasks |
| `Cycle detected in DAG` | You created a circular dependency (A→B→A) | Remove one of the circular dependencies |
| `FAILED: X has no DAG file` | The dag.json file wasn't written properly | Re-write the file ensuring it's valid JSON |

## Step 5: Re-run verification until it passes
- If ANY check fails, read the error output carefully and fix the specific issues
- Re-run the verification script after each fix
- Do NOT consider your work complete until the verification script prints: `Success: All DAGs are valid`

## Step 6: Final checklist before ending your turn
- [ ] JSON file exists at `{target_path}`
- [ ] JSON is valid (passes `python -c "import json; json.load(...)"`)
- [ ] `verify.py depends-on` passes with exit code 0
- [ ] `verify.py dags` passes with exit code 0
- [ ] Every task file in `docs/plan/tasks/{phase_filename}/` has a corresponding key in the JSON
- [ ] Every key in the JSON corresponds to an actual file on disk
- [ ] No circular dependencies exist

# ERROR HANDLING
- If a required input file is missing, print the exact path that was expected, then exit with a non-zero status. Do NOT create placeholder files or guess at content.
- If a verification script fails, read the error output carefully, fix the specific issues listed, and re-run. Do NOT skip verification.
- If you encounter malformed or unparseable content (broken JSON, invalid Markdown structure), report the exact location and nature of the error. Attempt to fix it if the fix is unambiguous; otherwise exit with a non-zero status.
- Never silently ignore errors. Every error must either be fixed or explicitly reported.
