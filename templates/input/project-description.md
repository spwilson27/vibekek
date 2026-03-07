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
- Visual verification should be done through golden image testing.
- Every requirement should be verified through automated tests.
- All code should be auto-formated and linted.
- All code should be documented with doc comments.
- All code should receive 90% line coverage from tests.
- A single python entrypoint script should be available after the first commit so we can verify every subsequent development cycle.
- The entrypoint script should support the following commands:
  - `./do setup` - Install all dev dependencies
  - `./do build` - Build for release
  - `./do test` - Run all tests
  - `./do lint` - Run all linters
  - `./do format` - Run all formatters
  - `./do coverage` - Run all coverage tools
  - `./do presubmit` - Run setup, formatters, linters, tests, coverage, and triggers `./do ci`.
  - `./do ci` - Automatically run all presubmit checks on CI runners by copying the working directory to make a temporary commit.
- Success of presubmit checks will gate commits and forward progress.  The `./do presubmit` command will enforce a timeout of 15 minutes.

We will leverage gitlab to run all of the presubmit checks. I will supply a
windows, mac and linux machine for us to validated on. We should be sure to
edit a CI/CD pipeline script to run all of these checks.

I also will want to leverage multiple AI agents at once in order to parallelize
the development process. For this reason tasks should be broken down into small,
independent chunks and each task description must clearly identify any requirements it is dependent on.
