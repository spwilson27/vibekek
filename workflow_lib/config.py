import os
import re
import json

from .constants import TOOLS_DIR

_CONFIG_FILE = os.path.join(TOOLS_DIR, "workflow.jsonc")


def load_config() -> dict:
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
    return load_config().get("serena", False)
