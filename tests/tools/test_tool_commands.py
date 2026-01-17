import os
from unittest.mock import MagicMock

import pytest

from aider.coders.agent_coder import AgentCoder
from aider.commands import Commands
from aider.io import InputOutput


@pytest.fixture
def mock_coder():
    coder = MagicMock(spec=AgentCoder)
    coder.root = os.getcwd()
    coder.io = MagicMock(spec=InputOutput)
    coder.get_rel_fname.side_effect = lambda x: x
    coder.abs_fnames = set()
    coder.abs_read_only_fnames = set()
    coder.abs_read_only_stubs_fnames = set()
    coder.mcp_tools = []
    coder.tool_manager = MagicMock()
    coder.tool_registry = {}
    coder.unloaded_standard_tools = {}
    return coder


@pytest.fixture
def commands(mock_coder):
    return Commands(mock_coder.io, mock_coder)


def test_tools_command_basic(commands, mock_coder):
    # Test that /tools command exists and can be called
    with MagicMock() as mock_output:
        mock_coder.io.tool_output = mock_output
        commands.cmd_tools("")
        # Verify it attempted to output something (even if just "Standard Tools:")
        assert mock_output.called
