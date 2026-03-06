# Goal

You are an expert Software Architect and Technical Reviewer. Your task is to review a previously generated Dependency Graph (DAG) for a set of technical tasks. You must ensure the DAG is logically sound, highly parallelizable, and free of circular dependencies.

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

## Proposed Dependency Graph (DAG)
<proposed_dag>
{proposed_dag}
</proposed_dag>

# Instructions

1.  **Analyze the Proposed DAG:** Review the `proposed_dag` JSON against the provided `tasks`.
2.  **Verify Logical Correctness:** Are all the listed dependencies actually necessary? If Task A and Task B can be built concurrently against an agreed-upon interface, they should NOT depend on each other. Only enforce strict prerequisites (e.g., Database Schema must be built before Database Queries, Interface definition before Implementation).
3.  **Detect Circular Dependencies:** Critically examine the graph to ensure no cycle exists. (e.g., A -> B -> C -> A). A circular dependency is fatal to parallel execution.
4.  **Detect Missing Dependencies:** Are there any obvious critical steps that must happen before another that were missed?
5.  **Refine and Output:** Your final output must be a corrected, perfectly formatted JSON object. 
    *   Keys are task IDs.
    *   Values are arrays of zero or more prerequisite task IDs.
    *   *Do NOT add any keys that did not exist in the Original Proposed DAG, and DO NOT remove any keys.*

# Output Format

Your final response MUST be enclosed within a json codeblock. No other text or markdown.

# CONSTRAINTS
- You MUST use your file editing tools to write the output directly into the provided `Target Output File` path. End your turn immediately once the file is written.

```json
{
  "task_id_1": [],
  "task_id_2": ["task_id_1"],
  "task_id_3": ["task_id_1"],
  "task_id_4": ["task_id_2", "task_id_3"]
}
```

<thinking>
1. Identify all nodes (task IDs).
2. For each edge (dependency), evaluate its strict necessity. Can these be built in parallel?
3. Trace paths to ensure no cycles exist.
4. Output the final refined JSON DAG.
</thinking>
