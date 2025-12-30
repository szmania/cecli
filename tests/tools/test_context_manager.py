import asyncio
import os
from unittest.mock import MagicMock

import pytest

from aider.coders.agent_coder import AgentCoder
from aider.tools.context_manager import Tool as ContextManagerTool


@pytest.fixture
def mock_coder():
    """
    Fixture to create a mock Coder instance for testing context manager.
    """
    coder = MagicMock(spec=AgentCoder)
    coder.abs_fnames = {"/path/to/file1.py", "/path/to/file2.js"}
    coder.abs_read_only_fnames = {"/path/to/readonly.txt"}
    coder.done_messages = [{"role": "user", "content": "hello"}] * 10

    def get_rel_fname(path):
        return os.path.basename(path)

    coder.get_rel_fname.side_effect = get_rel_fname
    coder.drop_rel_fname.return_value = True
    coder.get_context_summary.return_value = "Context summary"

    coder.main_model = MagicMock()
    coder.main_model.token_count.side_effect = lambda x: len(x)

    coder.io = MagicMock()
    coder.io.read_text.side_effect = lambda x: "content of " + os.path.basename(x)

    return coder


@pytest.mark.asyncio
async def test_context_manager_analyze(mock_coder):
    """
    Test the 'analyze' action of the ContextManager tool.
    """
    result = await ContextManagerTool.execute(mock_coder, action="analyze")
    assert result == "Context summary"
    mock_coder.get_context_summary.assert_called_once()


@pytest.mark.asyncio
async def test_context_manager_remove(mock_coder):
    """
    Test the 'remove' action of the ContextManager tool.
    """
    result = await ContextManagerTool.execute(mock_coder, action="remove", file_paths=["file1.py"])
    assert "Successfully removed files: file1.py" in result
    mock_coder.drop_rel_fname.assert_called_once_with("file1.py")


@pytest.mark.asyncio
async def test_context_manager_remove_not_in_context(mock_coder):
    """
    Test removing a file that is not in the context.
    """
    mock_coder.drop_rel_fname.return_value = False
    result = await ContextManagerTool.execute(mock_coder, action="remove", file_paths=["non_existent.py"])
    assert "Could not remove 'non_existent.py'" in result


@pytest.mark.asyncio
async def test_context_manager_remove_largest(mock_coder):
    """
    Test the 'remove_largest' action of the ContextManager tool.
    """
    def mock_read_text(path):
        if "file1.py" in path:
            return "long content" * 10
        return "short content"

    mock_coder.io.read_text.side_effect = mock_read_text

    result = await ContextManagerTool.execute(mock_coder, action="remove_largest", count=1)

    assert "Successfully removed largest files" in result
    assert "file1.py" in result
    mock_coder.drop_rel_fname.assert_called_once_with("file1.py")


@pytest.mark.asyncio
async def test_context_manager_clear_history(mock_coder):
    """
    Test the 'clear_history' action of the ContextManager tool.
    """
    mock_coder.done_messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]

    result = await ContextManagerTool.execute(mock_coder, action="clear_history", keep_percent=25)

    assert "Cleared 7 messages" in result
    assert len(mock_coder.done_messages) == 3
    assert mock_coder.done_messages[0]["content"] == "msg 7"


@pytest.mark.asyncio
async def test_context_manager_clear_empty_history(mock_coder):
    """
    Test clearing an already empty history.
    """
    mock_coder.done_messages = []
    result = await ContextManagerTool.execute(mock_coder, action="clear_history")
    assert "No messages in chat history to clear" in result


@pytest.mark.asyncio
async def test_context_manager_invalid_action(mock_coder):
    """
    Test an invalid action.
    """
    result = await ContextManagerTool.execute(mock_coder, action="invalid_action")
    assert "Error: Invalid action specified" in result
