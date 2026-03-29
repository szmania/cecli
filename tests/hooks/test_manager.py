"""Tests for HookManager."""

import json

import pytest

from cecli.hooks import BaseHook, HookManager, HookType


class MockHook(BaseHook):
    """Mock hook for unit testing."""

    type = HookType.START

    async def execute(self, coder, metadata):
        """Test execution."""
        return True


class MockPreToolHook(BaseHook):
    """Mock hook for pre_tool type."""

    type = HookType.PRE_TOOL

    async def execute(self, coder, metadata):
        """Test execution."""
        return True


class TestHookManager:
    """Test HookManager class."""

    def setup_method(self):
        """Set up test environment."""
        # Clear singleton instance
        HookManager._instance = None
        self.manager = HookManager()

    def test_singleton_pattern(self):
        """Test that HookManager is a singleton."""
        manager1 = HookManager()
        manager2 = HookManager()

        assert manager1 is manager2
        assert manager1._initialized is True
        assert manager2._initialized is True

    def test_register_hook(self):
        """Test hook registration."""
        hook = MockHook(name="test_hook")
        self.manager.register_hook(hook)

        assert self.manager.hook_exists("test_hook") is True
        assert "test_hook" in self.manager._hooks_by_name
        assert hook in self.manager._hooks_by_type[HookType.START.value]

    def test_register_duplicate_hook(self):
        """Test duplicate hook registration fails."""
        hook1 = MockHook(name="test_hook")
        hook2 = MockHook(name="test_hook")  # Same name

        self.manager.register_hook(hook1)

        with pytest.raises(ValueError, match="already exists"):
            self.manager.register_hook(hook2)

    def test_get_hooks(self):
        """Test getting hooks by type."""
        hook1 = MockHook(name="hook1", priority=10)
        hook2 = MockHook(name="hook2", priority=5)  # Higher priority
        hook3 = MockPreToolHook(name="hook3", priority=10)

        self.manager.register_hook(hook1)
        self.manager.register_hook(hook2)
        self.manager.register_hook(hook3)

        # Get start hooks
        start_hooks = self.manager.get_hooks(HookType.START.value)
        assert len(start_hooks) == 2

        # Should be sorted by priority (lower = higher priority)
        assert start_hooks[0].name == "hook2"  # priority 5
        assert start_hooks[1].name == "hook1"  # priority 10

        # Get pre_tool hooks
        pre_tool_hooks = self.manager.get_hooks(HookType.PRE_TOOL.value)
        assert len(pre_tool_hooks) == 1
        assert pre_tool_hooks[0].name == "hook3"

        # Get non-existent type
        no_hooks = self.manager.get_hooks("non_existent_type")
        assert len(no_hooks) == 0

    def test_get_all_hooks(self):
        """Test getting all hooks grouped by type."""
        hook1 = MockHook(name="hook1")
        hook2 = MockHook(name="hook2")
        hook3 = MockPreToolHook(name="hook3")

        self.manager.register_hook(hook1)
        self.manager.register_hook(hook2)
        self.manager.register_hook(hook3)

        all_hooks = self.manager.get_all_hooks()

        assert HookType.START.value in all_hooks
        assert HookType.PRE_TOOL.value in all_hooks
        assert len(all_hooks[HookType.START.value]) == 2
        assert len(all_hooks[HookType.PRE_TOOL.value]) == 1

    def test_hook_exists(self):
        """Test checking if hook exists."""
        hook = MockHook(name="test_hook")

        assert self.manager.hook_exists("test_hook") is False

        self.manager.register_hook(hook)

        assert self.manager.hook_exists("test_hook") is True
        assert self.manager.hook_exists("non_existent") is False

    def test_enable_disable_hook(self):
        """Test enabling and disabling hooks."""
        hook = MockHook(name="test_hook", enabled=False)
        self.manager.register_hook(hook)

        # Initially disabled
        start_hooks = self.manager.get_hooks(HookType.START.value)
        assert len(start_hooks) == 0  # Disabled hooks not returned

        # Enable hook
        result = self.manager.enable_hook("test_hook")
        assert result is True
        assert hook.enabled is True

        start_hooks = self.manager.get_hooks(HookType.START.value)
        assert len(start_hooks) == 1  # Now enabled

        # Disable hook
        result = self.manager.disable_hook("test_hook")
        assert result is True
        assert hook.enabled is False

        start_hooks = self.manager.get_hooks(HookType.START.value)
        assert len(start_hooks) == 0  # Disabled again

    def test_enable_nonexistent_hook(self):
        """Test enabling non-existent hook."""
        result = self.manager.enable_hook("non_existent")
        assert result is False

    def test_disable_nonexistent_hook(self):
        """Test disabling non-existent hook."""
        result = self.manager.disable_hook("non_existent")
        assert result is False

    def test_state_persistence(self, tmp_path):
        """Test hook state persistence."""
        # Create temporary state file
        state_file = tmp_path / "hooks_state.json"

        # Monkey-patch state file location
        self.manager._state_file = state_file

        # Create and register hooks
        hook1 = MockHook(name="hook1", enabled=True)
        hook2 = MockHook(name="hook2", enabled=False)

        self.manager.register_hook(hook1)
        self.manager.register_hook(hook2)

        # Save state
        self.manager._save_state()

        # Verify state file was created
        assert state_file.exists()

        # Load and verify state
        with open(state_file, "r") as f:
            state = json.load(f)

        assert state["hook1"] is True
        assert state["hook2"] is False

        # Create new manager instance to test loading
        HookManager._instance = None
        new_manager = HookManager()
        new_manager._state_file = state_file

        # Register hooks with new manager
        new_hook1 = MockHook(name="hook1", enabled=False)  # Default disabled
        new_hook2 = MockHook(name="hook2", enabled=True)  # Default enabled

        new_manager.register_hook(new_hook1)
        new_manager.register_hook(new_hook2)

        # Load state should override defaults
        new_manager._load_state()

        assert new_hook1.enabled is True  # Loaded from state
        assert new_hook2.enabled is False  # Loaded from state

    def test_clear(self):
        """Test clearing all hooks."""
        hook1 = MockHook(name="hook1")
        hook2 = MockHook(name="hook2")

        self.manager.register_hook(hook1)
        self.manager.register_hook(hook2)

        assert len(self.manager._hooks_by_name) == 2
        assert len(self.manager._hooks_by_type[HookType.START.value]) == 2

        self.manager.clear()

        assert len(self.manager._hooks_by_name) == 0
        assert len(self.manager._hooks_by_type) == 0

    @pytest.mark.asyncio
    async def test_call_hooks(self):
        """Test calling hooks."""

        # Create hooks with different return values
        class TrueHook(BaseHook):
            type = HookType.PRE_TOOL

            async def execute(self, coder, metadata):
                return True

        class FalseHook(BaseHook):
            type = HookType.PRE_TOOL

            async def execute(self, coder, metadata):
                return False

        class ErrorHook(BaseHook):
            type = HookType.PRE_TOOL

            async def execute(self, coder, metadata):
                raise ValueError("Test error")

        true_hook = TrueHook(name="true_hook")
        false_hook = FalseHook(name="false_hook")
        error_hook = ErrorHook(name="error_hook")

        self.manager.register_hook(true_hook)
        self.manager.register_hook(false_hook)
        self.manager.register_hook(error_hook)

        # Test with all hooks enabled
        result = await self.manager.call_hooks(HookType.PRE_TOOL.value, None, {})
        assert result is False  # false_hook returns False

        # Disable false_hook
        self.manager.disable_hook("false_hook")

        # Test with only true_hook and error_hook enabled
        result = await self.manager.call_hooks(HookType.PRE_TOOL.value, None, {})
        assert result is True  # Only true_hook returns True, error is caught

        # Disable all hooks
        self.manager.disable_hook("true_hook")
        self.manager.disable_hook("error_hook")

        # Test with no enabled hooks
        result = await self.manager.call_hooks(HookType.PRE_TOOL.value, None, {})
        assert result is True  # No hooks to run = success
