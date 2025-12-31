import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aider.coders.agent_coder import AgentCoder
from aider.io import InputOutput
from aider.tools.create_tool import Tool as CreateTool


@pytest.fixture
def mock_coder():
    """
    Fixture to create a mock Coder instance for testing.
    """
    coder = MagicMock(spec=AgentCoder)
    coder.io = MagicMock(spec=InputOutput)
    coder.repo = MagicMock()
    coder.repo.root = "/test/repo"
    coder.tool_manager = MagicMock()
    coder.tool_manager.load_tool_async = AsyncMock(return_value=(True, "Loaded"))
    coder.main_model = MagicMock()
    coder.main_model.extra_params = {}
    coder.main_model.name = "test-model"
    return coder


@pytest.mark.asyncio
async def test_create_tool_success(mock_coder):
    """
    Test successful creation of a tool.
    """
    description = "a test tool"
    file_name = "test_tool.py"
    scope = "local"

    mock_completion = MagicMock()
    mock_completion.choices[0].message.content = "```python\nclass Tool:\n    pass\n```"

    with patch("aider.tools.create_tool.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion, \
         patch("os.path.exists", return_value=False), \
         patch("os.makedirs"), \
         patch("builtins.open", new_callable=MagicMock):

        async def mock_iter(_):
            yield MagicMock(choices=[MagicMock(delta=MagicMock(content="```python\nclass Tool:\n    pass\n```"))])

        mock_acompletion.return_value = mock_iter(mock_completion)

        result = await CreateTool.execute(mock_coder, description, file_name, scope)

        assert "Successfully created tool" in result
        mock_acompletion.assert_called_once()


@pytest.mark.asyncio
async def test_create_tool_filename_must_end_with_py(mock_coder):
    """
    Test that the filename must end with .py.
    """
    result = await CreateTool.execute(mock_coder, "desc", "test_tool.txt")
    assert "must end with '.py'" in result


@pytest.mark.asyncio
async def test_create_tool_filename_must_not_contain_path_separators(mock_coder):
    """
    Test that the filename must not contain path separators.
    """
    result = await CreateTool.execute(mock_coder, "desc", "path/to/tool.py")
    assert "must not contain path separators" in result


@pytest.mark.asyncio
async def test_create_tool_invalid_scope(mock_coder):
    """
    Test that the scope must be 'local' or 'global'.
    """
    result = await CreateTool.execute(mock_coder, "desc", "tool.py", scope="invalid")
    assert "must be either 'local' or 'global'" in result


@pytest.mark.asyncio
async def test_create_tool_no_repo_for_local_scope(mock_coder):
    """
    Test that a local tool cannot be created without a git repository.
    """
    mock_coder.repo = None
    result = await CreateTool.execute(mock_coder, "desc", "tool.py", scope="local")
    assert "Cannot create local tool without a git repository" in result


@pytest.mark.asyncio
async def test_create_tool_file_exists_load_it(mock_coder):
    """
    Test the flow where the user confirms to load an existing tool file.
    """
    mock_coder.io.confirm_ask = AsyncMock(return_value=True)
    with patch("os.path.exists", return_value=True):
        result = await CreateTool.execute(mock_coder, "desc", "tool.py")
        assert "Successfully loaded existing tool" in result
        mock_coder.tool_manager.load_tool_async.assert_called_once()


@pytest.mark.asyncio
async def test_create_tool_file_exists_cancel(mock_coder):
    """
    Test the flow where the user cancels the operation if the file exists.
    """
    mock_coder.io.confirm_ask = AsyncMock(return_value=False)
    with patch("os.path.exists", return_value=True):
        result = await CreateTool.execute(mock_coder, "desc", "tool.py")
        assert "Operation cancelled" in result
        mock_coder.tool_manager.load_tool_async.assert_not_called()


@pytest.mark.asyncio
async def test_create_tool_with_dependencies(mock_coder):
    """
    Test tool creation with dependencies.
    """
    description = "a test tool with deps"
    file_name = "dep_tool.py"
    scope = "local"
    tool_code = "```python\n# pip install requests\nimport requests\nclass Tool:\n    pass\n```"

    mock_completion = MagicMock()
    mock_completion.choices[0].message.content = tool_code

    mock_coder.io.confirm_ask = AsyncMock(return_value=True)
    mock_coder.tool_manager.local_tools_python = "/fake/python"

    with patch("aider.tools.create_tool.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion, \
         patch("os.path.exists", side_effect=lambda path: "requirements.txt" in path), \
         patch("os.makedirs"), \
         patch("builtins.open", new_callable=MagicMock), \
         patch("asyncio.to_thread", new_callable=AsyncMock, return_value=(0, "installed")):

        async def mock_iter(_):
            yield MagicMock(choices=[MagicMock(delta=MagicMock(content=tool_code))])

        mock_acompletion.return_value = mock_iter(mock_completion)

        result = await CreateTool.execute(mock_coder, description, file_name, scope)

        assert "Successfully created tool" in result
        assert "Dependencies installed" in result
