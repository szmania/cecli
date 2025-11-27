import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from aider.coders import Coder
from aider.commands import Commands
from aider.io import InputOutput
from aider.models import Model


class TestToolManager(TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.tempdir = tempfile.mkdtemp()
        os.chdir(self.tempdir)
        self.GPT35 = Model("gpt-3.5-turbo")

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tempdir, ignore_errors=True)

    async def test_tool_manager_e2e(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        # 1. Create a tool
        tool_description = "a tool to list files in a directory"
        tool_file_name = "list_files.py"
        commands.cmd_tools_create(f"{tool_file_name} {tool_description}")

        # Verify tool is created and loaded
        self.assertIn("list_files", coder.tool_manager.tools)
        tool_path = Path(coder.tool_manager.local_tool_dir) / tool_file_name
        self.assertTrue(tool_path.exists())

        # 2. List tools
        with mock.patch.object(io, "tool_output") as mock_tool_output:
            commands.cmd_tools("")
            mock_tool_output.assert_any_call("- list_files")

        # 3. Execute the tool
        with mock.patch.object(io, "tool_output") as mock_tool_output:
            result = await coder.execute_tool("list_files", {"directory": "."})
            self.assertIn(tool_file_name, result)

        # 4. Unload the tool
        commands.cmd_tools_unload("list_files")
        self.assertNotIn("list_files", coder.tool_manager.tools)

        # 5. Load the tool
        commands.cmd_tools_load(str(tool_path))
        self.assertIn("list_files", coder.tool_manager.tools)

        # 6. Move the tool to global scope
        commands.cmd_tools_move("list_files global")
        global_tool_path = Path(coder.tool_manager.global_tool_dir) / tool_file_name
        self.assertTrue(global_tool_path.exists())
        self.assertNotIn("list_files", coder.tool_manager.tools)  # Should be unloaded after move

        # 7. Load from global scope
        coder.tool_manager.load_tools()
        self.assertIn("list_files", coder.tool_manager.tools)

    async def test_create_tool_invalid_name(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        with mock.patch.object(io, "tool_error") as mock_tool_error:
            commands.cmd_tools_create("invalid-name.py a tool")
            mock_tool_error.assert_called_with("Invalid tool name. Use snake_case.")

    async def test_load_non_existent_tool(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        with mock.patch.object(io, "tool_error") as mock_tool_error:
            commands.cmd_tools_load("non_existent_tool.py")
            mock_tool_error.assert_called_with("Tool file not found: non_existent_tool.py")

    async def test_unload_non_loaded_tool(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        with mock.patch.object(io, "tool_error") as mock_tool_error:
            commands.cmd_tools_unload("non_loaded_tool")
            mock_tool_error.assert_called_with("Tool not found: non_loaded_tool")

    async def test_move_non_loaded_tool(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        with mock.patch.object(io, "tool_error") as mock_tool_error:
            commands.cmd_tools_move("non_loaded_tool global")
            mock_tool_error.assert_called_with("Tool not found: non_loaded_tool")

    async def test_move_tool_invalid_scope(self):
        io = InputOutput(pretty=False, fancy_input=False, yes=True)
        coder = await Coder.create(self.GPT35, None, io)
        commands = Commands(io, coder)

        commands.cmd_tools_create("my_tool.py a tool")

        with mock.patch.object(io, "tool_error") as mock_tool_error:
            commands.cmd_tools_move("my_tool invalid_scope")
            mock_tool_error.assert_called_with(
                "Invalid scope: invalid_scope. Use 'local' or 'global'."
            )
