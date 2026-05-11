"""Tests for the Copilot coding-agent assignee name."""

from jira_webhook import COPILOT_ASSIGNEE


def test_copilot_assignee_name():
    """COPILOT_ASSIGNEE must match the correct GitHub bot login."""
    assert COPILOT_ASSIGNEE == "copilot-swe-agent[bot]"
