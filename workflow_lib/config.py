"""Configuration loader for ``workflow.jsonc``.

This module reads the project-level ``workflow.jsonc`` file and exposes
typed accessors for individual feature flags.  The file uses JSONC format
(JSON with ``//`` line comments), which is stripped before parsing so that
standard :mod:`json` can be used.

Example ``workflow.jsonc``::

    {
      // Enable Serena MCP server integration for code intelligence.
      "serena": true
    }
"""

import os
import re
import json
from typing import Any, Dict

from .constants import TOOLS_DIR

_CONFIG_FILE = os.path.join(TOOLS_DIR, "workflow.jsonc")


def load_config() -> Dict[str, Any]:
    """Read and parse ``workflow.jsonc``, returning its contents as a dict.

    Line comments (``// …``) are stripped before JSON parsing so that the
    JSONC superset is handled without an external dependency.  Any I/O or
    parse error causes an empty dict to be returned rather than crashing.

    :returns: Parsed configuration mapping, or ``{}`` on any error or when
        the file does not exist.
    :rtype: dict
    """
    if not os.path.exists(_CONFIG_FILE):
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        # Strip // line comments before parsing
        stripped = re.sub(r"//[^\n]*", "", raw)
        return json.loads(stripped)
    except Exception:
        return {}


def get_serena_enabled() -> bool:
    """Return whether the Serena MCP server integration is enabled.

    Reads the ``"serena"`` key from ``workflow.jsonc``.  Defaults to
    ``False`` when the key is absent or the config file cannot be loaded.

    :returns: ``True`` if Serena is opted in, ``False`` otherwise.
    :rtype: bool
    """
    return bool(load_config().get("serena", False))
