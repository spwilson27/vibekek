import sys
import os
import pytest
from unittest.mock import patch, MagicMock, mock_open

# Add .tools to sys.path so we can import workflow
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import workflow

def test_get_gitlab_remote_url_found():
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "origin\tgit@gitlab.lan:mrwilson/dreamer.git (fetch)\norigin\tgit@gitlab.lan:mrwilson/dreamer.git (push)\n"
        mock_run.return_value = mock_res
        
        url = workflow.get_gitlab_remote_url("/fake/root")
        assert url == "git@gitlab.lan:mrwilson/dreamer.git"
        
def test_get_gitlab_remote_url_not_found():
    with patch('subprocess.run') as mock_run:
        mock_res = MagicMock()
        mock_res.stdout = "origin\tgit@github.com:foo/bar.git (fetch)\n"
        mock_run.return_value = mock_res
        
        url = workflow.get_gitlab_remote_url("/fake/root")
        assert url == "http://gitlab.lan/mrwilson/dreamer"

def test_phase_sort_key():
    assert workflow.phase_sort_key("phase_1/01_foo") == (1, 1)
    assert workflow.phase_sort_key("phase_2/10_bar") == (2, 10)
    assert workflow.phase_sort_key("invalid/01_foo") == (0, 1)
    assert workflow.phase_sort_key("phase_x/xx_foo") == (0, 0)
    assert workflow.phase_sort_key("invalid") == (999, 999)

def test_load_workflow_state():
    with patch('os.path.exists', return_value=True):
        m = mock_open(read_data='{"completed_tasks": ["phase_1/01_foo"]}')
        with patch('builtins.open', m):
            state = workflow.load_workflow_state()
            assert "completed_tasks" in state
            assert state["completed_tasks"] == ["phase_1/01_foo"]

def test_load_workflow_state_empty():
    with patch('os.path.exists', return_value=False):
        state = workflow.load_workflow_state()
        assert state == {"completed_tasks": [], "merged_tasks": []}

