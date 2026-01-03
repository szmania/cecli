"""
Central registry for managing all prompts in YAML format.

This module implements a YAML-based prompt inheritance system where:
1. base.yml contains default prompts
2. Specific YAML files can override/extend base.yml
3. No Python prompt classes needed
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class PromptRegistry:
    """Central registry for loading and managing prompts from YAML files."""

    _instance = None
    _prompts_cache: Dict[str, Dict[str, Any]] = {}
    _base_prompts: Optional[Dict[str, Any]] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(PromptRegistry, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self._prompts_dir = Path(__file__).parent / "../../prompts"
            self._initialized = True

    def _load_yaml_file(self, file_path: Path) -> Dict[str, Any]:
        """Load a YAML file and return its contents."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing YAML file {file_path}: {e}")

    def _get_base_prompts(self) -> Dict[str, Any]:
        """Load and cache base.yml prompts."""
        if self._base_prompts is None:
            base_path = self._prompts_dir / "base.yml"
            self._base_prompts = self._load_yaml_file(base_path)
        return self._base_prompts

    def _merge_prompts(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively merge override dict into base dict."""
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_prompts(result[key], value)
            else:
                result[key] = value

        return result

    def get_prompt(self, prompt_name: str) -> Dict[str, Any]:
        """
        Get prompts for a specific prompt type.

        Args:
            prompt_name: Name of the prompt type (e.g., "agent", "editblock", "wholefile")

        Returns:
            Dictionary containing all prompt attributes for the specified type
        """
        # Check cache first
        if prompt_name in self._prompts_cache:
            return self._prompts_cache[prompt_name]

        # Load base prompts
        base_prompts = self._get_base_prompts()

        # Load specific prompt file if it exists
        prompt_path = self._prompts_dir / f"{prompt_name}.yml"
        specific_prompts = self._load_yaml_file(prompt_path)

        # Merge base with specific overrides
        merged_prompts = self._merge_prompts(base_prompts, specific_prompts)

        # Cache the result
        self._prompts_cache[prompt_name] = merged_prompts

        return merged_prompts

    def reload_prompts(self):
        """Clear cache and reload all prompts from disk."""
        self._prompts_cache.clear()
        self._base_prompts = None

    def list_available_prompts(self) -> list[str]:
        """List all available prompt types."""
        prompts = []
        for file_path in self._prompts_dir.glob("*.yml"):
            if file_path.name != "base.yml":
                prompts.append(file_path.stem)
        return sorted(prompts)


# Global instance for easy access
registry = PromptRegistry()
