# ORIGINAL PROJECT DESCRIPTION (Primary source of truth — do not add scope beyond this)
<project_description>
{description_ctx}
</project_description>

# Requirements
<requirements>
{requirements_ctx}
</requirements>

# Task

You are a Build & CI Engineer. Your job is to generate a project-specific
`harness.py` script that will be mounted **read-only** at `/harness.py` inside
every agent container starting from phase 1.

The harness is the **authoritative verification gate**. Agents cannot modify it.
It must enforce every testable requirement from the project description and
requirements documents above.

## Architecture

The harness calls each step in order. After the hardcoded checks, it invokes
an optional hook from `.agent/harness_hooks.py <step>` so that agents can
**add** project-specific setup (e.g. installing dependencies) but can never
**skip** the built-in checks.

The steps are:

1. **setup** — hook only (agents install deps via `.agent/harness_hooks.py setup`)
2. **fmt** — formatting checks + hook
3. **lint** — linting checks + hook
4. **test** — run the full test suite + hook
5. **build** — build for release + hook
6. **coverage** — run coverage, enforce hardcoded thresholds + hook

## Output

Write the file to: `{target_path}`

The script must:

1. **Be self-contained** — only use Python stdlib + tools available in the
   project's tech stack (e.g. `cargo`, `npm`, `pytest`).
2. **Hardcode all thresholds** — coverage percentages, lint severity levels,
   required test suites, etc. Agents must not be able to relax these.
3. **Call `.agent/harness_hooks.py <step>`** after every built-in check so
   agents can add checks but never bypass built-in ones.
4. **Exit non-zero** on any failure with a clear `[HARNESS]` prefixed message.
5. **Support `--setup-only`** flag to run only the setup step.
6. **Match the project's tech stack** — use the correct build, test, lint, and
   coverage commands for the languages and frameworks described above.
7. **Enforce every requirement** that can be verified automatically — map each
   requirement ID to the step that validates it (add a comment mapping).

Use the template at `.tools/templates/harness.py` as a starting reference for
the structure and helper functions, but adapt all commands and thresholds to
match this specific project.
