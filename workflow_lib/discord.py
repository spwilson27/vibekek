"""Discord webhook notifications for critical workflow failures.

Sends a message to a Discord channel via webhook when the workflow
encounters a critical failure that halts execution.

Configure by setting ``"discord_webhook"`` in ``.workflow.jsonc``::

    {
      "discord_webhook": "https://discord.com/api/webhooks/..."
    }
"""

import json
import os
import subprocess
import threading
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from .config import load_config
from .constants import ROOT_DIR


def _get_webhook_url() -> Optional[str]:
    """Return the Discord webhook URL from config, or None if not set."""
    return load_config().get("discord_webhook") or os.environ.get("DISCORD_WEBHOOK_URL")


def _get_project_name() -> str:
    """Return a short project identifier for the notification."""
    return os.path.basename(ROOT_DIR)


def notify_failure(message: str, *, context: str = "") -> None:
    """Send a critical failure notification to Discord.

    Does nothing if no webhook URL is configured.  Runs in a daemon thread
    so it never blocks the workflow.

    :param message: Short failure description.
    :param context: Optional extra detail (task ID, phase name, etc.).
    """
    url = _get_webhook_url()
    if not url:
        return

    project = _get_project_name()
    embed = {
        "title": f"\u274c Workflow Failure: {project}",
        "description": message,
        "color": 0xFF0000,  # red
    }
    if context:
        embed["fields"] = [{"name": "Details", "value": context[:1024], "inline": False}]

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")

    def _send() -> None:
        try:
            req = Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "AxelWorkflow/1.0"})
            urlopen(req, timeout=10)
        except Exception:
            pass  # Best-effort; don't crash the workflow over a notification

    t = threading.Thread(target=_send, daemon=True)
    t.start()
