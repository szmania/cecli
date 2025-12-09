"""
Tests for aider/helpers/skills.py
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aider.helpers.skills import SkillsManager


class TestSkills(unittest.TestCase):
    """Test suite for skills helper module."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_skills_manager_initialization(self):
        """Test that SkillsManager initializes correctly."""
        # Test with empty directory paths
        manager = SkillsManager([])
        self.assertEqual(manager.directory_paths, [])
        self.assertIsNone(manager.include_list)
        self.assertEqual(manager.exclude_list, set())
        self.assertIsNone(manager.git_root)
        # Test _loaded_skills is initialized as empty set
        self.assertEqual(manager._loaded_skills, set())

        # Test with directory paths
        manager = SkillsManager(["/tmp/test"])
        self.assertEqual(len(manager.directory_paths), 1)
        self.assertIsInstance(manager.directory_paths[0], Path)
        self.assertEqual(manager._loaded_skills, set())

        # Test with include/exclude lists
        manager = SkillsManager(
            ["/tmp/test"],
            include_list=["skill1", "skill2"],
            exclude_list=["skill3"],
            git_root="/tmp",
        )
        self.assertEqual(manager.include_list, {"skill1", "skill2"})
        self.assertEqual(manager.exclude_list, {"skill3"})
        self.assertEqual(manager.git_root, Path("/tmp").expanduser().resolve())
        self.assertEqual(manager._loaded_skills, set())

    def test_create_and_parse_skill(self):
        """Test creating a skill and parsing its metadata."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()

        # Create SKILL.md with proper format
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill

These are the main instructions.
""")

        # Create references directory
        ref_dir = skill_dir / "references"
        ref_dir.mkdir()
        (ref_dir / "api.md").write_text("# API Documentation")

        # Create scripts directory
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "setup.sh").write_text("#!/bin/bash\necho 'Setup script'")

        # Create assets directory
        assets_dir = skill_dir / "assets"
        assets_dir.mkdir()
        (assets_dir / "icon.png").write_bytes(b"fake_png_data")

        # Test loading the complete skill
        manager = SkillsManager([self.temp_dir])
        skill_content = manager.get_skill_content("test-skill")

        self.assertIsNotNone(skill_content)
        self.assertEqual(skill_content.metadata.name, "test-skill")
        self.assertEqual(skill_content.metadata.description, "A test skill")
        self.assertEqual(
            skill_content.instructions, "# Test Skill\n\nThese are the main instructions."
        )

        # Check references - should be Path objects
        self.assertEqual(len(skill_content.references), 1)
        self.assertIn("api.md", skill_content.references)
        self.assertIsInstance(skill_content.references["api.md"], Path)
        self.assertEqual(skill_content.references["api.md"].name, "api.md")

        # Check scripts - should be Path objects
        self.assertEqual(len(skill_content.scripts), 1)
        self.assertIn("setup.sh", skill_content.scripts)
        self.assertIsInstance(skill_content.scripts["setup.sh"], Path)
        self.assertEqual(skill_content.scripts["setup.sh"].name, "setup.sh")

        # Check assets - should be Path objects
        self.assertEqual(len(skill_content.assets), 1)
        self.assertIn("icon.png", skill_content.assets)
        self.assertIsInstance(skill_content.assets["icon.png"], Path)
        self.assertEqual(skill_content.assets["icon.png"].name, "icon.png")

        # Test that skill was NOT added to _loaded_skills (only load_skill() does that)
        self.assertNotIn("test-skill", manager._loaded_skills)
        self.assertEqual(manager._loaded_skills, set())

    def test_skill_summary_loader(self):
        """Test the skill_summary_loader function."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()

        # Create SKILL.md
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill for validation
---

# Test Skill

Test content.
""")
        # Test the skill summary loader (class method)
        summary = SkillsManager.skill_summary_loader([self.temp_dir])

        # Check that the summary contains expected information
        self.assertIn("Found 1 skill(s)", summary)
        self.assertIn("Skill: test-skill", summary)
        self.assertIn("Description: A test skill for validation", summary)

        # Test with include list
        summary = SkillsManager.skill_summary_loader([self.temp_dir], include_list=["test-skill"])
        self.assertIn("Found 1 skill(s)", summary)

        # Test with exclude list
        summary = SkillsManager.skill_summary_loader([self.temp_dir], exclude_list=["test-skill"])
        self.assertIn("No skills found", summary)

    def test_resolve_skill_directories(self):
        """Test the resolve_skill_directories function."""
        # Test with absolute path
        paths = SkillsManager.resolve_skill_directories([self.temp_dir])
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0], Path(self.temp_dir).resolve())

        # Test with relative path and git root
        paths = SkillsManager.resolve_skill_directories(["./test-dir"], git_root=self.temp_dir)
        # Should not resolve because directory doesn't exist
        self.assertEqual(len(paths), 0)

        # Create the directory and test again
        test_dir = Path(self.temp_dir) / "test-dir"
        test_dir.mkdir()
        paths = SkillsManager.resolve_skill_directories(["./test-dir"], git_root=self.temp_dir)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0], test_dir.resolve())

        # Test with non-existent path
        paths = SkillsManager.resolve_skill_directories(["/non-existent/path"])
        self.assertEqual(len(paths), 0)

    def test_remove_skill(self):
        """Test the remove_skill instance method."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()

        # Create SKILL.md
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill

Test content.
""")

        # Create a mock coder with agent mode
        mock_coder = MagicMock()
        mock_coder.edit_format = "agent"
        mock_coder.skills_includelist = []
        mock_coder.skills_excludelist = []

        # Create skills manager with coder reference
        manager = SkillsManager([self.temp_dir], coder=mock_coder)

        # First add the skill
        result = manager.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' loaded successfully", result)
        self.assertIn("test-skill", manager._loaded_skills)

        # Test removing a skill that exists
        result = manager.remove_skill("test-skill")
        self.assertEqual("Skill 'test-skill' removed successfully.", result)
        self.assertNotIn("test-skill", manager._loaded_skills)

        # Test removing the same skill again (should say not loaded)
        result = manager.remove_skill("test-skill")
        self.assertEqual("Skill 'test-skill' is not loaded.", result)

        # Test removing a skill not in include list (but not loaded)
        mock_coder2 = MagicMock()
        mock_coder2.edit_format = "agent"
        mock_coder2.skills_includelist = []
        mock_coder2.skills_excludelist = []

        manager2 = SkillsManager([self.temp_dir], coder=mock_coder2)
        result = manager2.remove_skill("test-skill")
        self.assertEqual("Skill 'test-skill' is not loaded.", result)

        # Test without coder reference
        manager_no_coder = SkillsManager([self.temp_dir])
        result = manager_no_coder.remove_skill("test-skill")
        self.assertEqual("Error: Skills manager not connected to a coder instance.", result)

        # Test not in agent mode
        mock_coder3 = MagicMock()
        mock_coder3.edit_format = "other-mode"
        mock_coder3.skills_includelist = ["test-skill"]
        mock_coder3.skills_excludelist = []

        manager3 = SkillsManager([self.temp_dir], coder=mock_coder3)
        result = manager3.remove_skill("test-skill")
        self.assertEqual("Error: Skill removal is only available in agent mode.", result)

        # Test with empty skill name
        mock_coder4 = MagicMock()
        mock_coder4.edit_format = "agent"
        mock_coder4.skills_includelist = []
        mock_coder4.skills_excludelist = []

        manager4 = SkillsManager([self.temp_dir], coder=mock_coder4)
        result = manager4.remove_skill("")
        self.assertEqual("Error: Skill name is required.", result)

    def test_load_skill(self):
        """Test the add_skill instance method."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()

        # Create SKILL.md
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill

Test content.
""")

        # Create a mock coder with agent mode
        mock_coder = MagicMock()
        mock_coder.edit_format = "agent"
        mock_coder.skills_includelist = []
        mock_coder.skills_excludelist = []

        # Create skills manager with coder reference
        manager = SkillsManager([self.temp_dir], coder=mock_coder)

        # Test adding a skill that exists
        result = manager.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' loaded successfully", result)
        self.assertIn("test-skill", manager._loaded_skills)

        # Test adding the same skill again (should say already loaded)
        result = manager.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' is already loaded", result)

        # Test adding a non-existent skill
        result = manager.load_skill("non-existent-skill")
        self.assertIn(
            "Error: Skill 'non-existent-skill' not found in configured directories.", result
        )
        self.assertNotIn("non-existent-skill", manager._loaded_skills)

        # Test with skill in exclude list (should still work since add_skill doesn't check exclude list)
        mock_coder2 = MagicMock()
        mock_coder2.edit_format = "agent"
        mock_coder2.skills_includelist = []
        mock_coder2.skills_excludelist = ["test-skill"]

        manager2 = SkillsManager([self.temp_dir], coder=mock_coder2)
        result = manager2.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' loaded successfully", result)
        self.assertIn("test-skill", manager2._loaded_skills)

        # Test without coder reference
        manager_no_coder = SkillsManager([self.temp_dir])
        result = manager_no_coder.load_skill("test-skill")
        self.assertEqual("Error: Skills manager not connected to a coder instance.", result)

        # Test not in agent mode
        mock_coder3 = MagicMock()
        mock_coder3.edit_format = "other-mode"
        mock_coder3.skills_includelist = []
        mock_coder3.skills_excludelist = []

        manager3 = SkillsManager([self.temp_dir], coder=mock_coder3)
        result = manager3.load_skill("test-skill")
        self.assertEqual("Error: Skill loading is only available in agent mode.", result)

    def test_get_skill_content_does_not_add_to_loaded_skills(self):
        """Test that get_skill_content() does NOT add to _loaded_skills."""
        # Create two skill directory structures
        skill_dir1 = Path(self.temp_dir) / "skill1"
        skill_dir1.mkdir()
        skill_md1 = skill_dir1 / "SKILL.md"
        skill_md1.write_text("""---
name: skill1
description: First test skill
---

# Skill 1

Test content.
""")

        skill_dir2 = Path(self.temp_dir) / "skill2"
        skill_dir2.mkdir()
        skill_md2 = skill_dir2 / "SKILL.md"
        skill_md2.write_text("""---
name: skill2
description: Second test skill
---

# Skill 2

Test content.
""")

        # Create skills manager
        manager = SkillsManager([self.temp_dir])

        # Test initial state
        self.assertEqual(manager._loaded_skills, set())

        # Get first skill content
        skill1 = manager.get_skill_content("skill1")
        self.assertIsNotNone(skill1)
        self.assertEqual(manager._loaded_skills, set())  # Should NOT be added

        # Get second skill content
        skill2 = manager.get_skill_content("skill2")
        self.assertIsNotNone(skill2)
        self.assertEqual(manager._loaded_skills, set())  # Should NOT be added

        # Get non-existent skill (should not add to _loaded_skills)
        skill3 = manager.get_skill_content("nonexistent")
        self.assertIsNone(skill3)
        self.assertEqual(manager._loaded_skills, set())

        # Get same skill again (should not add to _loaded_skills)
        skill1_again = manager.get_skill_content("skill1")
        self.assertIsNotNone(skill1_again)
        self.assertEqual(manager._loaded_skills, set())

    def test_get_skills_content_only_returns_loaded_skills(self):
        """Test that get_skills_content() only returns skills in _loaded_skills."""
        # Create two skill directory structures
        skill_dir1 = Path(self.temp_dir) / "skill1"
        skill_dir1.mkdir()
        skill_md1 = skill_dir1 / "SKILL.md"
        skill_md1.write_text("""---
name: skill1
description: First test skill
---

# Skill 1

Test content.
""")

        skill_dir2 = Path(self.temp_dir) / "skill2"
        skill_dir2.mkdir()
        skill_md2 = skill_dir2 / "SKILL.md"
        skill_md2.write_text("""---
name: skill2
description: Second test skill
---

# Skill 2

Test content.
""")

        # Create skills manager
        manager = SkillsManager([self.temp_dir])

        # Test with no loaded skills
        content = manager.get_skills_content()
        self.assertIsNone(content)

        # Load only skill1 via load_skill() (requires mock coder)
        mock_coder = MagicMock()
        mock_coder.edit_format = "agent"
        mock_coder.skills_includelist = []
        mock_coder.skills_excludelist = []
        manager.coder = mock_coder

        result = manager.load_skill("skill1")
        self.assertIn("Skill 'skill1' loaded successfully", result)
        content = manager.get_skills_content()
        self.assertIsNotNone(content)
        self.assertIn("skill1", content)
        self.assertNotIn("skill2", content)

        # Load skill2 as well
        result = manager.load_skill("skill2")
        self.assertIn("Skill 'skill2' loaded successfully", result)
        content = manager.get_skills_content()
        self.assertIsNotNone(content)
        self.assertIn("skill1", content)
        self.assertIn("skill2", content)

    def test_add_skill_updates_loaded_skills(self):
        """Test that load_skill() updates _loaded_skills."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill

Test content.
""")

        # Create a mock coder with agent mode
        mock_coder = MagicMock()
        mock_coder.edit_format = "agent"
        mock_coder.skills_includelist = []
        mock_coder.skills_excludelist = []

        # Create skills manager
        manager = SkillsManager([self.temp_dir], coder=mock_coder)

        # Test initial state
        self.assertEqual(manager._loaded_skills, set())

        # Add skill via load_skill() (simulating /load-skill command)
        result = manager.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' loaded successfully", result)
        self.assertIn("test-skill", manager._loaded_skills)

        # Test get_skills_content returns the skill
        content = manager.get_skills_content()
        self.assertIsNotNone(content)
        self.assertIn("test-skill", content)

    def test_remove_skill_updates_loaded_skills(self):
        """Test that remove_skill() updates _loaded_skills."""
        # Create a skill directory structure
        skill_dir = Path(self.temp_dir) / "test-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: test-skill
description: A test skill
---

# Test Skill

Test content.
""")

        # Create a mock coder with agent mode
        mock_coder = MagicMock()
        mock_coder.edit_format = "agent"
        mock_coder.skills_includelist = []
        mock_coder.skills_excludelist = []

        # Create skills manager and load the skill first via load_skill()
        manager = SkillsManager([self.temp_dir], coder=mock_coder)
        result = manager.load_skill("test-skill")
        self.assertIn("Skill 'test-skill' loaded successfully", result)
        self.assertIn("test-skill", manager._loaded_skills)

        # Remove the skill
        result = manager.remove_skill("test-skill")
        self.assertEqual("Skill 'test-skill' removed successfully.", result)
        self.assertNotIn("test-skill", manager._loaded_skills)

        # Test get_skills_content returns None
        content = manager.get_skills_content()
        self.assertIsNone(content)

    def test_skill_not_loaded_when_get_skill_content_fails(self):
        """Test that skill is not added to _loaded_skills when get_skill_content() fails."""
        # Create a skill directory structure with invalid SKILL.md (no frontmatter)
        skill_dir = Path(self.temp_dir) / "invalid-skill"
        skill_dir.mkdir()
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""# Invalid Skill

No frontmatter, so get_skill_content() should fail.
""")

        # Create skills manager
        manager = SkillsManager([self.temp_dir])

        # Try to get invalid skill content
        skill = manager.get_skill_content("invalid-skill")
        self.assertIsNone(skill)
        self.assertEqual(manager._loaded_skills, set())

        # Test get_skills_content returns None
        content = manager.get_skills_content()
        self.assertIsNone(content)


if __name__ == "__main__":
    unittest.main()
