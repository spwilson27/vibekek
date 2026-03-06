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

1.  **Analyze Dependencies:** Carefully read through each task provided in the `<tasks>` section. Identify strict logical dependencies:
    *   **Data/State Dependency:** Does Task B require the database schema created by Task A?
    *   **Interface Dependency:** Does Task B call an API endpoint or use a class defined in Task A?
    *   **Setup Dependency:** Does Task B require configuration or infrastructure provisioned by Task A?
2.  **Avoid Circular Dependencies:** Ensure that your dependencies form a Directed Acyclic Graph (DAG). Task A cannot depend on Task B if Task B depends on Task A.
3.  **Optimize for Parallelism:** Only add a dependency if it is *strictly* necessary. If two components (e.g., a frontend UI component and a deep backend service) can be developed independently based on an agreed-upon interface in another task, do not make them depend on each other. They should only depend on the task that defines the interface. This allows multiple agents to work on tasks concurrently.
4.  **Format:** Your output must be ONLY a valid JSON object.
    *   The keys of the JSON object must be the precise IDs of the tasks (e.g., the directory name of the task, like `01_project_planning`).
    *   The value for each key must be a JSON array of strings, where each string is the precise ID of a task that MUST be completed BEFORE the keyed task can begin.
    *   If a task has no prerequisites, its value should be an empty array `[]`.

# Output Format

Your final response MUST be enclosed within a json codeblock. No other text.

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
1. Re-read all tasks carefully.
2. For each task, ask: "What MUST exist before this can be started safely by an independent agent?"
3. Map these dependencies using the exact task IDs.
4. Double check for circular dependencies.
5. Create the JSON output.
</thinking>
