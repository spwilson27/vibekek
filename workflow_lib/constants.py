"""Package-wide constants and shared state for the workflow library.

Defines file-system paths derived from the location of this package, the
master document catalogue (``DOCS``), and imports the requirement-parsing
utilities from the project-level ``verify_requirements`` script.

Module-level constants
-----------------------

.. data:: TOOLS_DIR
    Absolute path to the ``.tools/`` directory (parent of this package).

.. data:: ROOT_DIR
    Absolute path to the project root (parent of ``TOOLS_DIR``).

.. data:: GEN_STATE_FILE
    Path to the JSON file that persists planning-phase state
    (``ProjectContext.state``).

.. data:: WORKFLOW_STATE_FILE
    Path to the JSON file that persists implementation-run state
    (completed / merged tasks).

.. data:: REPLAN_STATE_FILE
    Path to the JSON file that persists replan metadata (blocked tasks,
    history, etc.).

.. data:: ignore_file_lock
    :class:`threading.Lock` that serialises writes to AI runner ignore files
    so concurrent agents do not corrupt them.

.. data:: DOCS
    Ordered list of planning document descriptors.  Each entry is a dict
    with keys: ``id``, ``type`` (``"research"`` or ``"spec"``), ``name``,
    ``desc``, and ``prompt_file``.
"""

import os
import sys
import threading

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)
from verify_requirements import parse_requirements, REQ_REGEX  # noqa: E402

INPUT_DIR = os.path.join(ROOT_DIR, "input")
GEN_STATE_FILE = os.path.join(TOOLS_DIR, ".gen_state.json")
WORKFLOW_STATE_FILE = os.path.join(TOOLS_DIR, ".workflow_state.json")
REPLAN_STATE_FILE = os.path.join(TOOLS_DIR, ".replan_state.json")

ignore_file_lock = threading.Lock()

DOCS = [
    # Research
    {"id": "market_research", "type": "research", "name": "Market Research Report", "desc": "Analyze the problem space and create a market research report.", "prompt_file": "research_market.md"},
    {"id": "competitive_analysis", "type": "research", "name": "Competitive Analysis Report", "desc": "Analyze the competition and create a competitive analysis report.", "prompt_file": "research_competitive_analysis.md"},
    {"id": "tech_landscape", "type": "research", "name": "Technology Landscape Report", "desc": "Analyze the available technologies and create a technology landscape report.", "prompt_file": "research_technical_analysis.md"},
    {"id": "user_research", "type": "research", "name": "User Research Report", "desc": "Analyze potential users and create a user research report.", "prompt_file": "research_user_research.md"},

    # Specs
    {"id": "1_prd", "type": "spec", "name": "PRD (Product Requirements Document)", "desc": "Create a Product Requirements Document (PRD).", "prompt_file": "spec_prd.md"},
    {"id": "2_tas", "type": "spec", "name": "TAS (Technical Architecture Specification)", "desc": "Create a Technical Architecture Specification (TAS).", "prompt_file": "spec_tas.md"},
    {"id": "3_mcp_design", "type": "spec", "name": "MCP and AI Development Design", "desc": "Create an MCP and AI Development Design document.", "prompt_file": "spec_mcp_design.md"},
    {"id": "4_user_features", "type": "spec", "name": "User Features", "desc": "Create a User Features document describing user journeys and expectations.", "prompt_file": "spec_user_features.md"},
    {"id": "5_security_design", "type": "spec", "name": "Security Design", "desc": "Create a Security Design document detailing risks and security architectures.", "prompt_file": "spec_security_design.md"},
    {"id": "6_ui_ux_architecture", "type": "spec", "name": "UI/UX Architecture", "desc": "Create a UI/UX Architecture document.", "prompt_file": "spec_ui_ux_architecture.md"},
    {"id": "7_ui_ux_design", "type": "spec", "name": "UI/UX Design", "desc": "Create a UI/UX Design document.", "prompt_file": "spec_ui_ux_design.md"},
    {"id": "8_risks_mitigation", "type": "spec", "name": "Risks and Mitigation", "desc": "Create a Risks and Mitigation document.", "prompt_file": "spec_risks_mitigation.md"},
    {"id": "9_project_roadmap", "type": "spec", "name": "Project Roadmap", "desc": "Create a Project Roadmap.", "prompt_file": "spec_project_roadmap.md"},
]
