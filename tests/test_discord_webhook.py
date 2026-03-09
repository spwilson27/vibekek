"""Optional test to validate the Discord webhook configuration.

Skipped when no webhook URL is configured (via .workflow.jsonc or
DISCORD_WEBHOOK_URL env var).  When present, sends a test embed to
verify the webhook is reachable and accepts messages.

Run explicitly:
    python -m pytest tests/test_discord_webhook.py -v
"""

import json
import os
import sys
import unittest
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Allow importing workflow_lib from .tools/
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tools")
sys.path.insert(0, TOOLS_DIR)

from workflow_lib.config import load_config


def _get_webhook_url():
    return load_config().get("discord_webhook") or os.environ.get("DISCORD_WEBHOOK_URL")


class TestDiscordWebhook(unittest.TestCase):

    @unittest.skip("Disabled: causes Discord rate limiting during test runs")
    def test_webhook_sends_test_message(self):
        """Validate that the configured Discord webhook accepts a test message."""
        url = _get_webhook_url()
        if not url:
            self.skipTest("No discord_webhook configured in .workflow.jsonc or DISCORD_WEBHOOK_URL env var")

        self.assertTrue(
            url.startswith("https://discord.com/api/webhooks/"),
            f"Webhook URL doesn't look like a Discord webhook: {url[:40]}..."
        )

        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        payload = json.dumps({
            "embeds": [{
                "title": f"\u2705 Webhook Test: {project}",
                "description": "Discord webhook integration is working correctly.",
                "color": 0x00FF00,  # green
            }]
        }).encode("utf-8")

        req = Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "AxelWorkflow/1.0",
        })
        try:
            resp = urlopen(req, timeout=10)
            self.assertIn(resp.status, (200, 204), f"Unexpected status: {resp.status}")
        except HTTPError as e:
            self.fail(f"Discord webhook returned HTTP {e.code}: {e.read().decode()[:200]}")
        except URLError as e:
            self.fail(f"Failed to reach Discord webhook: {e.reason}")


if __name__ == "__main__":
    unittest.main()
