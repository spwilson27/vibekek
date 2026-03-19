# Project Description

<project> is designed from the ground up to support agentic development. It uses a
"Glass-Box" philosophy, supporting runtime use, development, debugging, and
testing via MCP server.

## High Level Features

- TODO
- MCP Server for AI agents to debug, profile, test, and interact with the applicaiton

## Non-Goals

- TODO

## Tech Stack

- TODO

## User Journeys

### The Developer of <project>

As the sole developer of <project>, I want to be able to develop exclusively
using agentic AI tools. From the start, the project should support agentic
development, including agentic debugging and profiling. It should do this
through a glass-box architecture which allows AI agents to understand the entire
internal state of the application and make changes to it. It should support MCP
as a first-class interface to the application. This means that the application
should be designed from the ground up to support MCP specifically for agents to
debug and profile with. A secondary goal is to support MCP for end-user support
to configure the project with their own agents.

I want to focus on developing an MVP that is strong and stable then build out
additional features from there. As a result, we will want to build out an MVP
first, and plan in checkpoints at the end of of the project for us to
investigate, and design all of the features we want to build out later on.

- All verification of the project should be completely automated through unit and E2E tests.
- Any visual verification should be done through golden image testing.
- Every requirement should be verified through automated tests.
- All code should be auto-formated and linted.
- All code should be documented with doc comments.
- All code should receive 90% line coverage from tests.
   - All code should receive 75% line coverage from e2e tests (tests through example binaries using public APIs)

### Verification Harness

Verification is handled by a read-only harness script (`/harness.py`) mounted
into agent containers. Agents **cannot** modify it. The harness runs hardcoded
checks (fmt, lint, build, test, coverage) and then calls optional hooks from
`.agent/harness_hooks.py` so agents can **add** project-specific checks without
relaxing the built-in ones.

- `python /harness.py setup` — Install any missing dependencies locally (in virtualenv, local cargo, npm, etc)
- `python /harness.py fmt`- Run auto code formatters
- `python /harness.py lint` — Run linters (cargo clippy, etc.)
- `python /harness.py build` — Build release binary(s)
- `python /harness.py test` — run 
- `python /harness.py coverage` — Full line coverage (both unit and integration) tests
- `python /harness.py presubmit` — Run setup -> fmt -> lint -> build -> coverage

**Phase 0 contract:** The very first implementation phase (`phase_0`) must
establish the project scaffolding so that `python /harness.py presubmit` passes
by the end of the phase. This includes:

1. A working build system (the project compiles/runs).
2. A `.agent/harness_hooks.py` that implements at least `setup`, `fmt`, `lint`,
   `build`, `test`, and `coverage` hooks appropriate for the project's tech
   stack (e.g. `cargo fmt`, `npm run lint`, `pytest`, etc.).
3. A minimal passing test suite so the coverage gate is satisfied.
4. Any CI/CD pipeline configuration.

Phase 0 tasks are **not** gated by the harness (presubmit is skipped). Starting
from phase 1, every task must pass `python /harness.py presubmit` before it can
be merged.

We will leverage gitlab to run all of the presubmit checks. I will supply a
windows, mac and linux machine for us to validated on. We should be sure to
edit a CI/CD pipeline script to run all of these checks.

