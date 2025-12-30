import os
import tempfile
import asyncio
from unittest.mock import MagicMock, patch

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
    return coder


@pytest.fixture
def commands(mock_coder):
    return Commands(mock_coder.io, mock_coder)


def test_tools_list(commands, mock_coder):
    mock_coder.tool_manager.list_tools.return_value = ["tool1", "tool2"]
    mock_coder.unloaded_standard_tools = {}
    mock_coder.tool_registry = {
        "tool3": MagicMock(SCHEMA={"function": {"name": "tool3", "description": "desc3"}}),
        "tool4": MagicMock(SCHEMA={"function": {"name": "tool4", "description": "desc4"}}),
    }

    with patch.object(commands.io, "tool_output") as mock_output:
        commands.cmd_tools("")
        
        # Check that the tool manager's list_tools method was called
        mock_coder.tool_manager.list_tools.assert_called_once()

        # Verify that the output contains the tool names
        output = "".join(call[0][0] for call in mock_output.call_args_list)
        assert "tool1" in output
        assert "tool2" in output
        assert "tool3" in output
        assert "tool4" in output


        
def test_tools_create(commands, mock_coder):
    with patch("aider.tools.create_tool.Tool.execute") as mock_create:
        asyncio.run(commands.cmd_tools_create("my-tool.py 'a new tool'"))
        mock_create.assert_called_once_with(
            coder=mock_coder, description="a new tool", file_name="my-tool.py", scope="local"
        )



def test_tools_load(commands, mock_coder):
    with patch.object(mock_coder.tool_manager, "load_tool_async") as mock_load:
        asyncio.run(commands.cmd_tools_load("my-tool.py"))
        mock_load.assert_called_once_with("my-tool.py")



def test_tools_unload(commands, mock_coder):
    with patch.object(mock_coder.tool_manager, "unload_tool") as mock_unload:
        asyncio.run(commands.cmd_tools_unload("my-tool"))
        mock_unload.assert_called_once_with("my-tool")



def test_tools_mv(commands, mock_coder):
    with patch.object(mock_coder.tool_manager, "move_tool") as mock_move:
        asyncio.run(commands.cmd_tools_mv("my-tool local"))
        mock_move.assert_called_once_with("my-tool", "local")



def test_tools_edit(commands, mock_coder):
    with patch.object(commands, "cmd_add") as mock_add:
        asyncio.run(commands.cmd_tools_edit("my-tool"))
        mock_add.assert_called_once_with("my-tool", is_tool_file=True)



def test_tools_rm(commands, mock_coder):
    with patch.object(mock_coder.tool_manager, "remove_tool") as mock_remove:
        asyncio.run(commands.cmd_tools_rm("my-tool"))
        mock_remove.assert_called_once_with("my-tool")



def test_tools_reload(commands, mock_coder):
    with patch.object(mock_coder.tool_manager, "reload_all_tools") as mock_reload:
        asyncio.run(commands.cmd_tools_reload(""))
        mock_reload.assert_called_once()