"""Tests for HookRegistry."""

from pathlib import Path

import pytest  # noqa: F401
import yaml

from cecli.hooks import BaseHook, HookManager, HookRegistry, HookType


class MockHook(BaseHook):
    """Mock hook for unit testing."""

    type = HookType.START

    async def execute(self, coder, metadata):
        """Test execution."""
        return True


class AnotherMockHook(BaseHook):
    """Another mock hook."""

    type = HookType.END

    async def execute(self, coder, metadata):
        """Test execution."""
        return True


class TestHookRegistry:
    """Test HookRegistry class."""

    def setup_method(self):
        """Set up test environment."""
        # Clear singleton instance
        HookManager._instance = None
        self.manager = HookManager()
        self.registry = HookRegistry(self.manager)

    def test_load_hooks_from_directory(self, tmp_path):
        """Test loading hooks from a directory."""
        # Create a test hook file
        hook_file = tmp_path / "test_hook.py"
        hook_file.write_text("""
from cecli.hooks import BaseHook, HookType

class TestHook(BaseHook):
    type = HookType.START

    async def execute(self, coder, metadata):
        return True

class AnotherHook(BaseHook):
    type = HookType.END

    async def execute(self, coder, metadata):
        return True
""")

        # Load hooks from directory
        loaded = self.registry.load_hooks_from_directory(tmp_path)

        assert len(loaded) == 2
        assert "TestHook" in loaded
        assert "AnotherHook" in loaded

        # Verify hooks were registered
        assert self.manager.hook_exists("TestHook")
        assert self.manager.hook_exists("AnotherHook")

    def test_load_hooks_from_empty_directory(self, tmp_path):
        """Test loading hooks from empty directory."""
        loaded = self.registry.load_hooks_from_directory(tmp_path)

        assert loaded == []

    def test_load_hooks_from_nonexistent_directory(self):
        """Test loading hooks from non-existent directory."""
        non_existent = Path("/non/existent/directory")
        loaded = self.registry.load_hooks_from_directory(non_existent)

        assert loaded == []

    def test_load_hooks_from_config(self, tmp_path):
        """Test loading hooks from YAML configuration."""
        # Create a test hook file first
        hook_file = tmp_path / "test_hook.py"
        hook_file.write_text("""
from cecli.hooks import BaseHook, HookType

class MyStartHook(BaseHook):
    type = HookType.START

    async def execute(self, coder, metadata):
        return True
""")

        # Create YAML config
        config_file = tmp_path / "hooks.yml"
        config = {
            "hooks": {
                "start": [
                    {
                        "name": "MyStartHook",
                        "file": str(hook_file),
                        "priority": 5,
                        "enabled": True,
                        "description": "Test start hook",
                    }
                ],
                "end": [
                    {
                        "name": "cleanup_hook",
                        "command": "echo 'Cleanup at {timestamp}'",
                        "priority": 10,
                        "enabled": False,
                        "description": "Test cleanup hook",
                    }
                ],
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        # Load hooks from config
        loaded = self.registry.load_hooks_from_config(config_file)

        assert len(loaded) == 2
        assert "MyStartHook" in loaded
        assert "cleanup_hook" in loaded

        # Verify hooks were registered
        assert self.manager.hook_exists("MyStartHook")
        assert self.manager.hook_exists("cleanup_hook")

        # Verify properties
        start_hook = self.manager._hooks_by_name["MyStartHook"]
        assert start_hook.priority == 5
        assert start_hook.enabled is True
        assert start_hook.description == "Test start hook"

        cleanup_hook = self.manager._hooks_by_name["cleanup_hook"]
        assert cleanup_hook.priority == 10
        assert cleanup_hook.enabled is False
        assert cleanup_hook.description == "Test cleanup hook"

    def test_load_hooks_from_invalid_config(self, tmp_path):
        """Test loading hooks from invalid YAML."""
        config_file = tmp_path / "hooks.yml"
        config_file.write_text("invalid: yaml: [")

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_empty_config(self, tmp_path):
        """Test loading hooks from empty config."""
        config_file = tmp_path / "hooks.yml"
        config_file.write_text("")

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_config_missing_hooks_key(self, tmp_path):
        """Test loading hooks from config missing 'hooks' key."""
        config_file = tmp_path / "hooks.yml"
        config = {"other_key": "value"}

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_config_invalid_hook_type(self, tmp_path):
        """Test loading hooks with invalid hook type."""
        config_file = tmp_path / "hooks.yml"
        config = {
            "hooks": {
                "invalid_type": [  # Not a valid HookType
                    {"name": "test_hook", "command": "echo test", "priority": 10, "enabled": True}
                ]
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_config_missing_name(self, tmp_path):
        """Test loading hooks with missing name."""
        config_file = tmp_path / "hooks.yml"
        config = {
            "hooks": {
                "start": [
                    {
                        # Missing name
                        "command": "echo test",
                        "priority": 10,
                        "enabled": True,
                    }
                ]
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_config_missing_file_or_command(self, tmp_path):
        """Test loading hooks with missing file or command."""
        config_file = tmp_path / "hooks.yml"
        config = {
            "hooks": {
                "start": [
                    {
                        "name": "test_hook",
                        "priority": 10,
                        "enabled": True,
                        # Missing file or command
                    }
                ]
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_hooks_from_config_nonexistent_file(self, tmp_path):
        """Test loading hooks with non-existent file."""
        config_file = tmp_path / "hooks.yml"
        config = {
            "hooks": {
                "start": [
                    {
                        "name": "test_hook",
                        "file": "/non/existent/file.py",
                        "priority": 10,
                        "enabled": True,
                    }
                ]
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        loaded = self.registry.load_hooks_from_config(config_file)

        assert loaded == []

    def test_load_default_hooks(self, tmp_path, monkeypatch):
        """Test loading hooks from default locations."""
        # Mock home directory
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        # Create .cecli directory structure
        cecli_dir = mock_home / ".cecli"
        cecli_dir.mkdir()
        hooks_dir = cecli_dir / "hooks"
        hooks_dir.mkdir()

        # Create a hook file
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text("""
from cecli.hooks import BaseHook, HookType

class MyHook(BaseHook):
    type = HookType.START

    async def execute(self, coder, metadata):
        return True
""")

        # Create YAML config
        config_file = cecli_dir / "hooks.yml"
        config = {
            "hooks": {
                "end": [
                    {"name": "config_hook", "command": "echo test", "priority": 10, "enabled": True}
                ]
            }
        }

        with open(config_file, "w") as f:
            yaml.dump(config, f)

        # Monkey-patch Path.home
        monkeypatch.setattr(Path, "home", lambda: mock_home)

        # Load default hooks
        loaded = self.registry.load_default_hooks()

        assert len(loaded) == 2
        assert "MyHook" in loaded
        assert "config_hook" in loaded

    def test_reload_hooks(self, tmp_path, monkeypatch):
        """Test reloading hooks."""
        # Mock home directory
        mock_home = tmp_path / "home"
        mock_home.mkdir()

        # Create .cecli directory structure
        cecli_dir = mock_home / ".cecli"
        cecli_dir.mkdir()
        hooks_dir = cecli_dir / "hooks"
        hooks_dir.mkdir()

        # Create a hook file
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text("""
from cecli.hooks import BaseHook, HookType

class MyHook(BaseHook):
    type = HookType.START

    async def execute(self, coder, metadata):
        return True
""")

        # Monkey-patch Path.home
        monkeypatch.setattr(Path, "home", lambda: mock_home)

        # Load default hooks
        loaded1 = self.registry.load_default_hooks()
        assert len(loaded1) == 1
        assert "MyHook" in loaded1

        # Update the hook file
        hook_file.write_text("""
from cecli.hooks import BaseHook, HookType

class NewHook(BaseHook):
    type = HookType.END

    async def execute(self, coder, metadata):
        return True
""")

        # Reload hooks
        loaded2 = self.registry.reload_hooks()

        assert len(loaded2) == 1
        assert "NewHook" in loaded2
        assert "MyHook" not in loaded2  # Old hook should be gone

        # Verify manager was cleared
        assert not self.manager.hook_exists("MyHook")
        assert self.manager.hook_exists("NewHook")
