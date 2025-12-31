import os
import tempfile
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from aider.coders.agent_coder import AgentCoder
from aider.commands import Commands
from aider.io import InputOutput
from aider.tool_manager import ToolManager


@pytest.fixture
def mock_coder():
    coder = MagicMock(spec=AgentCoder)
    coder.root = os.getcwd()
    coder.io = MagicMock(spec=InputOutput)
    coder.get_rel_fname.side_effect = lambda x: x
    coder.tool_manager = MagicMock(spec=ToolManager)
    coder.tool_manager.tools = {}
    coder.tool_registry = {}
    return coder


@pytest.fixture
def commands(mock_coder):
    return Commands(mock_coder.io, mock_coder)


def test_tools_list(commands, mock_coder):
    mock_coder.tool_manager.tools = {
        "tool1": {
            "name": "tool1",
            "definition": {"function": {"description": "desc1"}},
            "file_path": "path/to/tool1.py",
        },
        "tool2": {
            "name": "tool2",
            "definition": {"function": {"description": "desc2"}},
            "file_path": "path/to/tool2.py",
        },
    }
    mock_coder.tool_manager._get_local_tools_dir.return_value = "path/to"
    mock_coder.unloaded_standard_tools = {}
    mock_coder.tool_registry = {
        "tool3": MagicMock(SCHEMA={"function": {"name": "tool3", "description": "desc3"}}),
        "tool4": MagicMock(SCHEMA={"function": {"name": "tool4", "description": "desc4"}}),
    }

    with patch.object(commands.io, "tool_output") as mock_output:
        commands.cmd_tools("")

        # Verify that the output contains the tool names
        output = "".join(call[0][0] for call in mock_output.call_args_list if call[0])
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
    mock_coder.tool_manager.find_tool.return_value = (True, "my-tool", "Found tool.")
    mock_coder.unloaded_standard_tools = {"my-tool": MagicMock()}
    asyncio.run(commands.cmd_tools_load("my-tool"))
    assert "my-tool" in mock_coder.tool_registry



def test_tools_unload(commands, mock_coder):
    mock_coder.tool_manager.find_tool.return_value = (True, "my-tool", "Found tool.")
    with patch.object(mock_coder.tool_manager, "unload_tool") as mock_unload:
        asyncio.run(commands.cmd_tools_unload("my-tool"))
        mock_unload.assert_called_once_with("my-tool")



def test_tools_mv(commands, mock_coder):
    mock_coder.tool_manager.find_tool.return_value = (True, "my-tool", "Found tool.")
    mock_coder.tool_manager.tools = {"my-tool": {"file_path": "/path/to/my-tool.py"}}
    with patch.object(mock_coder.tool_manager, "move_tool") as mock_move:
        asyncio.run(commands.cmd_tools_mv("my-tool local"))
        mock_move.assert_called_once_with("my-tool", "local")



def test_tools_edit(commands, mock_coder):
    mock_coder.tool_manager.find_tool.return_value = (True, "my-tool", "Found tool.")
    mock_coder.tool_manager.tools = {"my-tool": {"file_path": "/path/to/my-tool.py"}}
    with patch.object(commands, "cmd_add", new_callable=AsyncMock) as mock_add:
        asyncio.run(commands.cmd_tools_edit("my-tool"))
        mock_add.assert_called_once_with("/path/to/my-tool.py", is_tool_file=True)



def test_tools_rm(commands, mock_coder):
    mock_coder.tool_manager.find_tool.return_value = (True, "my-tool", "Found tool.")
    with patch.object(mock_coder.tool_manager, "remove_tool") as mock_remove:
        asyncio.run(commands.cmd_tools_rm("my-tool"))
        mock_remove.assert_called_once_with("my-tool")



def test_tools_reload(commands, mock_coder):
    mock_coder.tool_manager.tools = {
        "my-tool": {"name": "my-tool", "file_path": "path/to/my-tool.py"}
    }
    mock_coder.tool_manager._get_local_tools_dir.return_value = None
    mock_coder.tool_manager._get_global_tools_dir.return_value = None
    with patch("os.path.exists", return_value=True):
        asyncio.run(commands.cmd_tools_reload(""))
        mock_coder.tool_manager.unload_tool.assert_called_once_with("my-tool")
