# Workflow Tool

An AI-driven planning and implementation workflow that takes a project description from
idea to a fully-planned, parallel-executed codebase. It has two main phases:

1. **Plan** — multi-phase AI pipeline that produces research documents, specs, requirements,
   epics, tasks, and dependency DAGs.
2. **Run** — parallel implementation loop where AI agents work on tasks concurrently,
   each in an isolated git clone, with automatic squash-merge into `dev`.

---

## Workflow Overview

```mermaid
flowchart TD
    A([Start]) --> C["1. Setup<br/>workflow.py setup"]
    C --> C1[".tools/.venv/<br/>do, ci.py, .agent/<br/>input/"]

    C1 --> B["2. Describe project<br/>Edit input/ files"]

    B --> D["3. Plan<br/>workflow.py plan"]

    D --> P0["Phase 1-2 - Generate & flesh out docs<br/>docs/plan/specs/<br/>docs/plan/research/"]
    P0 --> P1a["Phase 3 - Final review"]
    P1a --> P1b["Phase 3A - Conflict resolution<br/>docs/plan/conflict_resolution.md"]
    P1b --> P1["Phase 3B - Adversarial review<br/>docs/plan/adversarial_review.md"]
    P1 --> P2["Phase 4 - Extract + merge + scope + order<br/>docs/plan/requirements/"]
    P2 --> P3["Phase 5 - Epics + shared components + contracts<br/>docs/plan/phases/<br/>docs/plan/shared_components.md<br/>docs/plan/interface_contracts.md"]
    P3 --> P4["Phase 6 - Tasks + review + reorder + integration tests<br/>docs/plan/tasks/phase_N/sub_epic/NN_task.md<br/>docs/plan/integration_test_plan.md"]
    P4 --> P5["Phase 7 - DAG<br/>docs/plan/tasks/phase_N/dag.json"]

    P5 --> E["4. Implement<br/>workflow.py run --jobs N"]

    E --> R1["Pick next ready task<br/>prerequisites met, not blocked"]
    R1 --> R2["Clone repo<br/>ai-phase-task branch"]
    R2 --> R3["Implementation agent<br/>Review agent"]
    R3 --> R4["Run ./do presubmit<br/>up to 3 attempts"]
    R4 -->|Pass| R5["Squash-merge into dev<br/>task marked complete"]
    R4 -->|Fail| R3
    R5 --> R6{More tasks?}
    R6 -->|Yes| R1
    R6 -->|Phase complete| R7{More phases?}
    R7 -->|Yes| R1
    R7 -->|No| Z([Done])
```

---

## Requirements

| Dependency | Purpose |
|---|---|
| Python 3.9+ | Runtime |
| `git` | Worktree and branch management |
| `gemini` CLI (or `claude` / `codex` / `copilot` / `opencode` / `cline` / `aider` / `qwen`) | AI backend |
| `uvx` *(optional)* | Serena MCP integration |

---

## Quick Start

### 1. Describe your project

Add one or more files to `.tools/input/`. Every file in this directory is read and
concatenated (sorted by filename) to form the project context passed to every AI step.
This is the single source of truth for all downstream planning and implementation.

```
.tools/input/
  project-description.md   # main description (required)
  tech-stack.md             # optional — tech constraints, language choices
  existing-api.md           # optional — existing systems to integrate with
  …                         # any other reference material
```

Files are injected with a `## <filename>` header so the AI can distinguish between them.

### 2. Set up the environment

```bash
python .tools/workflow.py setup
```

This creates `.tools/.venv/`, installs dependencies, and copies starter templates
(`.agent/`, `do`, `ci.py`, `tests/`) into the project root.

### 3. Run the planning pipeline

```bash
python .tools/workflow.py plan
```

Runs all planning phases in order and produces:

```
docs/plan/
  research/          # Market, competitive, tech landscape, user research
  specs/             # PRD, TAS, security design, UI/UX, roadmap, …
  requirements/      # Per-doc extracted requirements
  phases/            # Implementation epics (phase_1.md, phase_2.md, …)
  tasks/             # Atomic task files, grouped into sub-epics
    phase_1/
      <sub_epic>/
        01_task.md
        02_task.md
        …
      dag.json       # Dependency graph for this phase
    phase_2/
      …
  shared_components.md
  interface_contracts.md
  conflict_resolution.md
  adversarial_review.md
  integration_test_plan.md
requirements.md      # Master requirements list
```

#### Planning phases

| Phase | Description | Output |
|---|---|---|
| 1 | Generate planning documents (4 research + 9 specs) | `docs/plan/research/`, `docs/plan/specs/` |
| 2 | Section-by-section expansion of spec documents | (updates spec files in-place) |
| 3 | Final holistic consistency review | (updates spec files in-place) |
| 3A | Conflict resolution between documents | `docs/plan/conflict_resolution.md` |
| 3B | Adversarial scope-creep review | `docs/plan/adversarial_review.md` |
| 4A | Extract requirements per document | `docs/plan/requirements/` |
| 4B | Merge into master requirements + scope gate | `requirements.md` |
| 4C | Order requirements by dependency | `requirements.md` (reordered) |
| 5 | Generate implementation epics | `docs/plan/phases/` |
| 5B | Identify shared components | `docs/plan/shared_components.md` |
| 5C | Define interface contracts | `docs/plan/interface_contracts.md` |
| 6 | Break epics into tasks | `docs/plan/tasks/` |
| 6B | Review tasks for completeness | (updates task files) |
| 6C | Cross-phase review (2 passes) | (updates task files) |
| 6D | Task ordering validation (2 passes) | (validation report) |
| 6E | Integration test plan | `docs/plan/integration_test_plan.md` |
| 7A | Generate per-phase dependency DAGs | `docs/plan/tasks/phase_N/dag.json` |

The pipeline is resumable — each phase records its completion state and is
skipped on re-run.

**Re-run a specific phase:**

```bash
python .tools/workflow.py plan --phase 6-tasks --force
```

Phase slugs: `3a-conflicts`, `3b-adversarial`, `4-merge`, `4-scope`, `4-order`, `5-epics`,
`5b-components`, `5c-contracts`, `6-tasks`, `6b-review`, `6c-cross-review`, `6d-reorder`,
`6e-integration`, `7-dag`.

**Use a different AI backend:**

```bash
python .tools/workflow.py plan --backend claude
```

Available backends: `gemini` (default), `claude`, `codex`, `copilot`, `opencode`, `cline`, `aider`, `qwen`.

**Control retries and timeout:**

```bash
# No retries, 15-minute timeout per agent
python .tools/workflow.py plan --retries 0 --timeout 900

# Default: 3 retries, 10-minute (600s) timeout
python .tools/workflow.py plan
```

On timeout, the agent is killed and the phase is auto-retried (no user prompt).
On other failures, the user is prompted to retry, continue, or quit.

### 4. Implement in parallel

```bash
python .tools/workflow.py run --jobs 4
```

For each ready task (prerequisites met, not blocked):

1. Clones the repo into a temp directory on a dedicated branch (`ai-phase-<task>`).
2. Runs the **Implementation** agent, then the **Review** agent.
3. Runs `./do presubmit` (configurable via `--presubmit-cmd`) up to 3 times,
   feeding failures back to the Review agent.
4. Squash-merges the branch into `dev` via a temporary clone.
5. Records the task as completed and pushes `dev`.

Tasks in earlier phases act as a barrier — phase N must fully complete before
phase N+1 begins.

Progress is logged to `run_workflow.log` in the project root. Press **Ctrl-C** once for a
graceful drain (in-flight tasks finish); twice for immediate exit.

---

## Configuration

### `.workflow.jsonc`

```jsonc
{
  // Enable Serena MCP server integration for agent code intelligence.
  // When true, workflow seeds each clone with a Serena cache and rebuilds
  // it after each successful merge so agents have up-to-date code search.
  "serena": false
}
```

Set `"serena": true` to enable [Serena](https://github.com/oraios/serena) code-intelligence
in every task clone. Requires `uvx` on `PATH`. On first run, the Serena index is
bootstrapped from `dev`; it is refreshed after each successful merge.

### Presubmit command

The `run` command verifies each task with a shell command before merging.
Default: `./do presubmit`. Override with:

```bash
python .tools/workflow.py run --presubmit-cmd "pytest -x"
```

### Agent memory

`.agent/MEMORY.md` in the project root is injected into every implementation
agent's context. Use it to record architectural decisions, naming conventions,
and brittle areas so agents stay consistent across tasks.

The memory file is enforced to stay at 100 lines or fewer via `tests/test_memory_size.py`
(run by `./do presubmit`).

---

## Validation & Quality

### Requirement ID format

All requirement IDs follow the format `[{DOC_PREFIX}-REQ-{NUM}]`, e.g.:
- `[1_PRD-REQ-001]`, `[2_TAS-REQ-001]`, `[3_MCP_DESIGN-REQ-001]`

The prefix matches the source document's ID. This is validated by:
```bash
python .tools/verify_requirements.py --verify-req-format requirements.md
```

### Verification modes

| Flag | Purpose |
|---|---|
| `--verify-doc SOURCE EXTRACTED` | Check extracted requirements match source |
| `--verify-master` | Check master list covers all per-doc requirements |
| `--verify-phases MASTER PHASES_DIR` | Check all requirements are mapped to phases |
| `--verify-ordered MASTER ORDERED` | Check ordered file matches active requirements |
| `--verify-json PHASE JSON` | Check sub-epic grouping matches phase requirements |
| `--verify-tasks PHASES_DIR TASKS_DIR` | Check tasks cover all phase requirements |
| `--verify-dags TASKS_DIR` | Check DAGs are valid, consistent, and cycle-free |
| `--verify-req-format FILE` | Check IDs follow `[DOC_PREFIX-REQ-NNN]` format |
| `--verify-uniqueness DIR` | Check no requirement ID appears in multiple files |

### Startup validation

The orchestrator validates that all prompt template files exist before running any phase.
Missing prompts are reported and the run is aborted immediately rather than failing
hours into the pipeline.

### Prompt placeholder validation

Each prompt has a canonical set of required placeholders registered in
`workflow_lib/prompt_registry.py`. When `format_prompt_for()` is used, missing
placeholders are logged as warnings.

### Artifact validation

After key phases, the orchestrator validates that expected output files exist and are
non-empty. Missing or empty artifacts halt the pipeline.

### Per-phase DAG cycle detection

DAG cycles are detected both within each phase's `dag.json` and across the merged
master DAG. Orphan task files (`.md` files not tracked in any DAG) are treated as errors.

---

## Status & Replan Commands

### Check progress

```bash
python .tools/workflow.py status
```

Shows each task with a status icon:

| Icon | Meaning |
|---|---|
| `[x]` | Merged into `dev` |
| `[~]` | Completed, not yet merged |
| `[ ]` | Ready (all prerequisites met) |
| `[.]` | Waiting on prerequisites |
| `[B]` | Blocked |

### Validate plan artefacts

```bash
python .tools/workflow.py validate
```

Runs all `verify_requirements.py` checks (master list, phase coverage, task coverage,
DAGs, requirement format).

### Block / unblock a task

```bash
python .tools/workflow.py block phase_1/api/01_setup.md --reason "API design not finalised"
python .tools/workflow.py unblock phase_1/api/01_setup.md
```

Blocked tasks are skipped by `run` and do not block other tasks' prerequisites.

### Remove a task

```bash
python .tools/workflow.py remove phase_1/api/03_legacy.md
```

Deletes the file, removes it from `dag.json`, and warns about any orphaned requirements.

### Add a new task

```bash
python .tools/workflow.py add phase_1 api --desc "Add rate-limiting middleware"
```

AI-generates a new task file in the specified phase/sub-epic and rebuilds the DAG.

### Modify requirements

```bash
# Open requirements.md in $EDITOR
python .tools/workflow.py modify-req --edit

# Remove a requirement (moves it to a 'Removed' section)
python .tools/workflow.py modify-req --remove AUTH-005

# Add a requirement interactively
python .tools/workflow.py modify-req --add "New feature description"
```

### Regenerate DAGs

```bash
python .tools/workflow.py regen-dag              # all phases
python .tools/workflow.py regen-dag --phase phase_1  # single phase
```

Rebuilds `dag.json` for all phases (or a single `--phase`) — programmatically from
task `depends_on` metadata when available, AI-generated otherwise.

### Regenerate tasks

```bash
python .tools/workflow.py regen-tasks phase_1 --sub-epic api
```

Clears and regenerates task files for a sub-epic, then rebuilds the DAG.

### Regenerate shared components

```bash
python .tools/workflow.py regen-components
```

### Cascade after manual edits

After editing task files by hand:

```bash
python .tools/workflow.py cascade phase_1
```

Rescans tasks, checks requirement coverage, rebuilds the DAG, and validates.

---

## Directory Structure

```
.tools/
  workflow.py          # Entry point (delegates to workflow_lib/)
  .workflow.jsonc       # Configuration (serena toggle, etc.)
  verify_requirements.py  # Requirement verification script
  workflow_lib/        # Core library
    cli.py             # Argument parser + command dispatch
    orchestrator.py    # Planning phase sequencer
    phases.py          # Phase implementations (Phase1 … Phase7A)
    executor.py        # Parallel DAG execution engine
    replan.py          # Mid-run replan commands
    context.py         # Shared project context + AI runner wrapper
    runners.py         # AI backend adapters (Gemini, Claude, Codex, Copilot, Opencode, Cline, Aider, Qwen)
    state.py           # Workflow + replan state persistence
    config.py          # .workflow.jsonc loader
    constants.py       # Paths + document catalogue
    prompt_registry.py # Canonical prompt placeholder registry
  prompts/             # Prompt templates for every AI step
  input/
    project-description.md   # ← Edit this first
  templates/
    .agent/MEMORY.md   # Agent memory template
    do                 # Presubmit / build script template
    ci.py              # CI script template
    tests/             # Template test suite (memory size check)
    .mcp.json          # Serena MCP server config template
  tests/               # pytest test suite
  requirements.txt     # Python dependencies (pytest, coverage, mypy)
```

---

## Development

### Run tests

```bash
cd .tools
.venv/bin/python -m pytest tests/ -v
```

### Run with coverage

```bash
.venv/bin/python -m pytest tests/ --cov=workflow_lib --cov-report=term-missing
```

### Type checking

```bash
.venv/bin/python -m mypy workflow_lib/ --ignore-missing-imports
```

Or via the test suite:

```bash
.venv/bin/python -m pytest tests/test_mypy.py -v
```

---

## Troubleshooting

**Planning stopped mid-run** — re-run `python .tools/workflow.py plan`. Completed phases
are skipped automatically. Use `--phase <slug> --force` to re-run a specific phase.

**An agent timed out** — by default agents have a 10-minute timeout. Increase with
`--timeout 1200` (20 minutes). Timed-out agents are auto-retried up to `--retries` times.

**A task is stuck in a clone** — the clone directory is left on disk on failure for inspection.
Clean up with:

```bash
python .tools/clean-worktrees.py
```

**DAG deadlock during `run`** — use `python .tools/workflow.py status` to see which tasks
are waiting. Check for a cycle in the DAG or a blocked prerequisite, then use
`block`, `remove`, or `regen-dag` to resolve it. Per-phase cycle detection runs during
`validate` to catch these early.

**Scope creep in requirements** — run `python .tools/workflow.py validate` and review
`docs/plan/adversarial_review.md`. Use `modify-req --remove` to prune requirements,
then `cascade` to rebuild affected DAGs.

**Duplicate requirement IDs** — run `python .tools/verify_requirements.py --verify-uniqueness docs/plan/requirements/`
to find IDs that appear in multiple documents.

**Conflict between documents** — review `docs/plan/conflict_resolution.md` which is
generated by Phase 3A. Conflicts are resolved using a priority hierarchy:
Description > PRD > TAS > Other Specs > Research.
