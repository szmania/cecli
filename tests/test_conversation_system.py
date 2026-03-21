"""
Unit tests for the conversation system.
"""

import pytest

from cecli.helpers.conversation import (
    BaseMessage,
    ConversationChunks,
    ConversationFiles,
    ConversationManager,
    MessageTag,
)
from cecli.io import InputOutput


class TestCoder:
    """Simple test coder class for conversation system tests."""

    def __init__(self, io=None):
        self.abs_fnames = set()
        self.abs_read_only_fnames = set()
        self.edit_format = None
        self.context_management_enabled = False
        self.large_file_token_threshold = 1000
        self.io = io or InputOutput(yes=False)
        self.add_cache_headers = False  # Default to False for tests

    @property
    def done_messages(self):
        """Get DONE messages from ConversationManager."""
        return ConversationManager.get_messages_dict(MessageTag.DONE)

    @property
    def cur_messages(self):
        """Get CUR messages from ConversationManager."""
        return ConversationManager.get_messages_dict(MessageTag.CUR)


class TestBaseMessage:
    """Test BaseMessage class."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset conversation manager before each test."""
        ConversationManager.reset()
        ConversationFiles.reset()
        yield
        ConversationManager.reset()
        ConversationFiles.reset()

    def test_base_message_creation(self):
        """Test creating a BaseMessage instance."""
        message_dict = {"role": "user", "content": "Hello, world!"}
        message = BaseMessage(message_dict=message_dict, tag=MessageTag.CUR.value)

        assert message.message_dict == message_dict
        assert message.tag == MessageTag.CUR.value
        assert message.priority == 0  # Default priority
        assert message.message_id is not None
        assert message.mark_for_delete is None

    def test_base_message_validation(self):
        """Test message validation."""
        # Missing role should raise ValueError
        with pytest.raises(ValueError):
            BaseMessage(message_dict={"content": "Hello"}, tag=MessageTag.CUR.value)

        # Missing content and tool_calls should raise ValueError
        with pytest.raises(ValueError):
            BaseMessage(message_dict={"role": "user"}, tag=MessageTag.CUR.value)

        # Valid with tool_calls
        message_dict = {"role": "assistant", "tool_calls": [{"id": "1", "type": "function"}]}
        message = BaseMessage(message_dict=message_dict, tag=MessageTag.CUR.value)
        assert message.message_dict == message_dict

    def test_base_message_hash_generation(self):
        """Test hash generation for messages."""
        message_dict1 = {"role": "user", "content": "Hello"}
        message_dict2 = {"role": "user", "content": "Hello"}
        message_dict3 = {"role": "user", "content": "World"}

        message1 = BaseMessage(message_dict=message_dict1, tag=MessageTag.CUR.value)
        message2 = BaseMessage(message_dict=message_dict2, tag=MessageTag.CUR.value)
        message3 = BaseMessage(message_dict=message_dict3, tag=MessageTag.CUR.value)

        # Same content should have same hash
        assert message1.message_id == message2.message_id
        # Different content should have different hash
        assert message1.message_id != message3.message_id

    def test_base_message_expiration(self):
        """Test message expiration logic."""
        message_dict = {"role": "user", "content": "Hello"}

        # Message with no mark_for_delete should not expire
        message1 = BaseMessage(message_dict=message_dict, tag=MessageTag.CUR.value)
        assert not message1.is_expired()

        # Message with mark_for_delete = -1 should expire
        message2 = BaseMessage(
            message_dict=message_dict, tag=MessageTag.CUR.value, mark_for_delete=-1
        )
        assert message2.is_expired()

        # Message with mark_for_delete > 0 should not expire
        message3 = BaseMessage(
            message_dict=message_dict, tag=MessageTag.CUR.value, mark_for_delete=1
        )
        assert not message3.is_expired()


class TestConversationManager:
    """Test ConversationManager class."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset conversation manager before each test."""
        ConversationManager.reset()

        # Create a test coder with real InputOutput
        self.test_coder = TestCoder()

        # Initialize conversation system
        ConversationChunks.initialize_conversation_system(self.test_coder)
        yield
        ConversationManager.reset()

    def test_add_message(self):
        """Test adding messages to conversation manager."""
        message_dict = {"role": "user", "content": "Hello"}

        # Add first message
        message1 = ConversationManager.add_message(
            message_dict=message_dict,
            tag=MessageTag.CUR,
        )

        assert len(ConversationManager.get_messages()) == 1
        assert ConversationManager.get_messages()[0] == message1

        # Add same message again (should be idempotent)
        message2 = ConversationManager.add_message(
            message_dict=message_dict,
            tag=MessageTag.CUR,
        )

        assert message1 == message2  # Should return same message
        assert len(ConversationManager.get_messages()) == 1  # Still only one

        # Add different message
        message_dict2 = {"role": "assistant", "content": "Hi there!"}
        ConversationManager.add_message(
            message_dict=message_dict2,
            tag=MessageTag.CUR,
        )

        assert len(ConversationManager.get_messages()) == 2

    def test_add_message_with_force(self):
        """Test adding messages with force=True."""
        message_dict = {"role": "user", "content": "Hello"}

        # Add first message
        message1 = ConversationManager.add_message(
            message_dict=message_dict,
            tag=MessageTag.CUR,
            priority=100,
        )

        # Add same message with force=True and different priority
        message2 = ConversationManager.add_message(
            message_dict=message_dict,
            tag=MessageTag.CUR,
            priority=200,
            force=True,
        )

        assert message1 == message2  # Should be same object
        assert message2.priority == 200  # Priority should be updated

    def test_message_ordering(self):
        """Test that messages are ordered by priority and timestamp."""
        # Add messages with different priorities
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System message"},
            tag=MessageTag.SYSTEM,
            priority=0,  # Lowest priority = first
        )

        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message"},
            tag=MessageTag.CUR,
            priority=200,  # Higher priority = later
        )

        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message"},
            tag=MessageTag.CUR,
            priority=100,  # Medium priority = middle
        )

        messages = ConversationManager.get_messages()

        # Check ordering
        assert messages[0].message_dict["content"] == "System message"
        assert messages[1].message_dict["content"] == "Assistant message"
        assert messages[2].message_dict["content"] == "User message"

    def test_clear_tag(self):
        """Test clearing messages by tag."""
        # Add messages with different tags
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User 2"},
            tag=MessageTag.CUR,
        )

        assert len(ConversationManager.get_messages()) == 3

        # Clear CUR messages
        ConversationManager.clear_tag(MessageTag.CUR)

        messages = ConversationManager.get_messages()
        assert len(messages) == 1
        assert messages[0].message_dict["content"] == "System"

    def test_get_tag_messages(self):
        """Test getting messages by tag."""
        # Add messages with different tags
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User 2"},
            tag=MessageTag.CUR,
        )

        cur_messages = ConversationManager.get_tag_messages(MessageTag.CUR)
        assert len(cur_messages) == 2

        system_messages = ConversationManager.get_tag_messages(MessageTag.SYSTEM)
        assert len(system_messages) == 1

    def test_decrement_mark_for_delete(self):
        """Test decrementing mark_for_delete values."""
        # Add message with mark_for_delete
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "Temp message"},
            tag=MessageTag.CUR,
            mark_for_delete=0,  # Will expire after one decrement (0 -> -1)
        )

        assert len(ConversationManager.get_messages()) == 1

        # Decrement once
        ConversationManager.decrement_mark_for_delete()

        # Message should be removed
        assert len(ConversationManager.get_messages()) == 0

    def test_get_messages_dict(self):
        """Test getting message dictionaries for LLM consumption."""
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "Hello"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Hi!"},
            tag=MessageTag.CUR,
        )

        messages_dict = ConversationManager.get_messages_dict()

        assert len(messages_dict) == 2
        assert messages_dict[0]["role"] == "user"
        assert messages_dict[0]["content"] == "Hello"
        assert messages_dict[1]["role"] == "assistant"
        assert messages_dict[1]["content"] == "Hi!"

    def test_get_messages_dict_with_tag_filter(self):
        """Test getting message dictionaries filtered by tag."""
        # Add messages with different tags
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System message"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 2"},
            tag=MessageTag.DONE,
        )

        # Test getting all messages (no tag filter)
        all_messages = ConversationManager.get_messages_dict()
        assert len(all_messages) == 4

        # Test filtering by CUR tag
        cur_messages = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages) == 2
        assert all(msg["role"] in ["user", "assistant"] for msg in cur_messages)
        assert any(msg["content"] == "User message 1" for msg in cur_messages)
        assert any(msg["content"] == "Assistant message 1" for msg in cur_messages)

        # Test filtering by SYSTEM tag
        system_messages = ConversationManager.get_messages_dict(MessageTag.SYSTEM)
        assert len(system_messages) == 1
        assert system_messages[0]["role"] == "system"
        assert system_messages[0]["content"] == "System message"

        # Test filtering by DONE tag
        done_messages = ConversationManager.get_messages_dict(MessageTag.DONE)
        assert len(done_messages) == 1
        assert done_messages[0]["role"] == "user"
        assert done_messages[0]["content"] == "User message 2"

        # Test filtering by tag string (not enum)
        cur_messages_str = ConversationManager.get_messages_dict("cur")
        assert len(cur_messages_str) == 2

        # Test invalid tag handling
        with pytest.raises(ValueError):
            ConversationManager.get_messages_dict("invalid_tag")

    def test_debug_functionality(self):
        """Test debug mode and message comparison functionality."""
        # First, disable debug to test enabling it
        ConversationManager.set_debug_enabled(False)

        # Add a message with debug disabled
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "Test message 1"},
            tag=MessageTag.CUR,
        )

        # Get messages dict (should not trigger debug comparison)
        messages_dict1 = ConversationManager.get_messages_dict()
        assert len(messages_dict1) == 1

        # Enable debug mode
        ConversationManager.set_debug_enabled(True)

        # Add another message
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Test response 1"},
            tag=MessageTag.CUR,
        )

        # Get messages dict again (should trigger debug comparison)
        messages_dict2 = ConversationManager.get_messages_dict()
        assert len(messages_dict2) == 2

        # Disable debug mode again
        ConversationManager.set_debug_enabled(False)

        # Add one more message
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "Test message 2"},
            tag=MessageTag.CUR,
        )

        # Get final messages dict
        messages_dict3 = ConversationManager.get_messages_dict()
        assert len(messages_dict3) == 3

        # Test debug_validate_state method
        assert ConversationManager.debug_validate_state()

        # Test debug_get_stream_info method
        stream_info = ConversationManager.debug_get_stream_info()
        assert "stream_length" in stream_info
        assert stream_info["stream_length"] == 3
        assert "hashes" in stream_info
        assert len(stream_info["hashes"]) == 3
        assert "tags" in stream_info
        assert "priorities" in stream_info

    def test_caching_functionality(self):
        """Test caching for tagged message dict queries."""
        # Clear any existing cache
        ConversationManager.clear_cache()

        # Add messages with different tags
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System message"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 2"},
            tag=MessageTag.DONE,
        )

        # First call to get CUR messages - should compute and cache
        cur_messages1 = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages1) == 2

        # Second call to get CUR messages - should use cache
        cur_messages2 = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages2) == 2
        assert cur_messages1 == cur_messages2  # Should be same object from cache

        # Call with reload=True - should bypass cache
        cur_messages3 = ConversationManager.get_messages_dict(MessageTag.CUR, reload=True)
        assert len(cur_messages3) == 2
        assert cur_messages1 == cur_messages3  # Content should be same

        # Get DONE messages - should compute and cache
        done_messages1 = ConversationManager.get_messages_dict(MessageTag.DONE)
        assert len(done_messages1) == 1

        # Get SYSTEM messages - should compute and cache
        system_messages1 = ConversationManager.get_messages_dict(MessageTag.SYSTEM)
        assert len(system_messages1) == 1

        # Add a new CUR message - should invalidate CUR cache
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 3"},
            tag=MessageTag.CUR,
        )

        # Get CUR messages again - should recompute (cache was invalidated)
        cur_messages4 = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages4) == 3  # Now has 3 messages

        # Clear tag should clear cache for that tag
        ConversationManager.clear_tag(MessageTag.CUR)

        # Get CUR messages after clear - should recompute
        cur_messages5 = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages5) == 0  # All CUR messages cleared

        # Test clear_cache method
        # Get DONE messages to populate cache
        done_messages2 = ConversationManager.get_messages_dict(MessageTag.DONE)
        assert len(done_messages2) == 1

        # Clear all cache
        ConversationManager.clear_cache()

        # Get DONE messages again - should recompute after cache clear
        done_messages3 = ConversationManager.get_messages_dict(MessageTag.DONE)
        assert len(done_messages3) == 1

        # Test reset also clears cache
        # Get SYSTEM messages to populate cache
        system_messages2 = ConversationManager.get_messages_dict(MessageTag.SYSTEM)
        assert len(system_messages2) == 1

        # Reset should clear everything including cache
        ConversationManager.reset()

        # Get SYSTEM messages after reset - should be empty
        system_messages3 = ConversationManager.get_messages_dict(MessageTag.SYSTEM)
        assert len(system_messages3) == 0

    def test_coder_properties(self):
        """Test that coder.done_messages and coder.cur_messages properties work."""
        # Create a test coder
        coder = TestCoder()

        # Initialize conversation system
        ConversationChunks.initialize_conversation_system(coder)

        # Add messages with different tags
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message 1"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message 2"},
            tag=MessageTag.DONE,
        )

        # Test coder.cur_messages property
        cur_messages = coder.cur_messages
        assert len(cur_messages) == 2
        assert cur_messages[0]["content"] == "User message 1"
        assert cur_messages[1]["content"] == "Assistant message 1"

        # Test coder.done_messages property
        done_messages = coder.done_messages
        assert len(done_messages) == 1
        assert done_messages[0]["content"] == "User message 2"

        # Test that properties return the same as direct ConversationManager calls
        assert cur_messages == ConversationManager.get_messages_dict(MessageTag.CUR)
        assert done_messages == ConversationManager.get_messages_dict(MessageTag.DONE)

    def test_cache_control_headers(self):
        """Test that cache control headers are only added when coder.add_cache_headers = True."""
        # Create a test coder with add_cache_headers = False (default)
        coder_false = TestCoder()
        coder_false.add_cache_headers = False
        ConversationChunks.initialize_conversation_system(coder_false)

        # Add some messages
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System message"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message"},
            tag=MessageTag.CUR,
        )

        # Get all messages (no tag filter) - should NOT add cache control headers
        messages_dict_false = ConversationManager.get_messages_dict()
        assert len(messages_dict_false) == 3

        # Check that no cache control headers were added
        for msg in messages_dict_false:
            content = msg.get("content")
            if isinstance(content, list):
                # If content is a list, check that no element has cache_control
                for element in content:
                    if isinstance(element, dict):
                        assert "cache_control" not in element
            elif isinstance(content, dict):
                # If content is a dict, check it doesn't have cache_control
                assert "cache_control" not in content

        # Reset and test with add_cache_headers = True
        ConversationManager.reset()

        coder_true = TestCoder()
        coder_true.add_cache_headers = True
        ConversationChunks.initialize_conversation_system(coder_true)

        # Add the same messages
        ConversationManager.add_message(
            message_dict={"role": "system", "content": "System message"},
            tag=MessageTag.SYSTEM,
        )
        ConversationManager.add_message(
            message_dict={"role": "user", "content": "User message"},
            tag=MessageTag.CUR,
        )
        ConversationManager.add_message(
            message_dict={"role": "assistant", "content": "Assistant message"},
            tag=MessageTag.CUR,
        )

        # Get all messages (no tag filter) - SHOULD add cache control headers
        messages_dict_true = ConversationManager.get_messages_dict()
        assert len(messages_dict_true) == 3

        # Check that cache control headers were added to specific messages
        # The system message (first) and last 2 messages should have cache control
        # In this case: system message (index 0), assistant message (index 2), user message (index 1)
        # Note: The last system message before the last 2 non-system messages gets cache control
        # Since we have system at index 0, and non-system at indices 1 and 2, system at index 0 gets cache control

        # Check system message (index 0) has cache control
        system_msg = messages_dict_true[0]
        assert isinstance(system_msg.get("content"), list)
        assert len(system_msg["content"]) == 1
        assert isinstance(system_msg["content"][0], dict)
        assert "cache_control" in system_msg["content"][0]

        # Check last message (index 2) has cache control
        last_msg = messages_dict_true[2]
        assert isinstance(last_msg.get("content"), list)
        assert len(last_msg["content"]) == 1
        assert isinstance(last_msg["content"][0], dict)
        assert "cache_control" in last_msg["content"][0]

        # Check second-to-last message (index 1) has cache control
        second_last_msg = messages_dict_true[1]
        assert isinstance(second_last_msg.get("content"), list)
        assert len(second_last_msg["content"]) == 1
        assert isinstance(second_last_msg["content"][0], dict)
        assert "cache_control" in second_last_msg["content"][0]

        # Test that filtered messages (with tag) don't get cache control headers
        cur_messages = ConversationManager.get_messages_dict(MessageTag.CUR)
        assert len(cur_messages) == 2
        # CUR messages should not have cache control when filtered by tag
        for msg in cur_messages:
            content = msg.get("content")
            # When filtered by tag, cache control should not be added
            # Content should be string, not list with cache control dict
            assert isinstance(content, str)
            assert content in ["User message", "Assistant message"]


class TestConversationFiles:
    """Test ConversationFiles class."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Reset conversation files before each test."""
        ConversationFiles.reset()

        # Create a test coder with real InputOutput
        self.test_coder = TestCoder()

        # Initialize conversation system
        ConversationChunks.initialize_conversation_system(self.test_coder)
        yield
        ConversationFiles.reset()

    def test_add_and_get_file_content(self, mocker):
        """Test adding and getting file content."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Test content")
            temp_file = f.name

        try:
            # Mock read_text to return file content
            def mock_read_text(filename, silent=False):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return None

            # Patch the read_text method on the coder's io
            mocker.patch.object(self.test_coder.io, "read_text", side_effect=mock_read_text)

            # Add file to cache
            content = ConversationFiles.add_file(temp_file)
            assert content == "Test content"

            # Get file content from cache
            cached_content = ConversationFiles.get_file_content(temp_file)
            assert cached_content == "Test content"

            # Get content for non-existent file
            non_existent_content = ConversationFiles.get_file_content("/non/existent/file")
            assert non_existent_content is None
        finally:
            # Clean up
            os.unlink(temp_file)

    def test_has_file_changed(self, mocker):
        """Test file change detection."""
        import os
        import tempfile
        import time

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Initial content")
            temp_file = f.name

        try:
            # Mock read_text to return file content
            def mock_read_text(filename, silent=False):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return None

            # Patch the read_text method on the coder's io
            mocker.patch.object(self.test_coder.io, "read_text", side_effect=mock_read_text)

            # Add file to cache
            ConversationFiles.add_file(temp_file)

            # File should not have changed yet
            assert not ConversationFiles.has_file_changed(temp_file)

            # Modify the file
            time.sleep(0.01)  # Ensure different mtime
            with open(temp_file, "w") as f:
                f.write("Modified content")

            # File should now be detected as changed
            assert ConversationFiles.has_file_changed(temp_file)
        finally:
            os.unlink(temp_file)
