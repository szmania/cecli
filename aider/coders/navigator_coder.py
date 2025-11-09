import ast
import asyncio
import base64
import json
import locale
import os
import platform
import re
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import importlib.util
import inspect
import jsonschema
from litellm import experimental_mcp_client

from aider import urls, utils
from aider.change_tracker import ChangeTracker
from aider.mcp.server import LocalServer
from aider.repo import ANY_GIT_ERROR
from aider.tools.base_tool import BaseAiderTool
from aider.tools.command import _execute_command
from aider.tools.command_interactive import _execute_command_interactive
from aider.tools.create_tool import CreateTool
from aider.tools.delete_block import _execute_delete_block, delete_block_schema
from aider.tools.delete_line import _execute_delete_line, delete_line_schema
from aider.tools.delete_lines import _execute_delete_lines, delete_lines_schema
from aider.tools.extract_lines import _execute_extract_lines, extract_lines_schema
from aider.tools.git import (
    GitDiff,
    git_diff_schema,
    GitLog,
    git_log_schema,
    GitShow,
    git_show_schema,
    GitStatus,
    git_status_schema,
)
from aider.tools.grep import _execute_grep
from aider.tools.indent_lines import _execute_indent_lines, indent_lines_schema
from aider.tools.insert_block import _execute_insert_block, insert_block_schema
from aider.tools.list_changes import _execute_list_changes, list_changes_schema
from aider.tools.ls import execute_ls
from aider.tools.make_editable import _execute_make_editable
from aider.tools.make_readonly import _execute_make_readonly
from aider.tools.remove import _execute_remove
from aider.tools.replace_all import _execute_replace_all, replace_all_schema
from aider.tools.replace_line import _execute_replace_line, replace_line_schema
from aider.tools.replace_lines import _execute_replace_lines, replace_lines_schema
from aider.tools.replace_text import _execute_replace_text, replace_text_schema
from aider.tools.show_numbered_context import (
    execute_show_numbered_context,
    show_numbered_context_schema,
)
from aider.tools.undo_change import _execute_undo_change, undo_change_schema
from aider.tools.update_todo_list import _execute_update_todo_list, update_todo_list_schema
from aider.tools.view import execute_view
from aider.tools.view_files_at_glob import execute_view_files_at_glob
from aider.tools.view_files_matching import execute_view_files_matching
from aider.tools.view_files_with_symbol import _execute_view_files_with_symbol

from .base_coder import ChatChunks, Coder
from .editblock_coder import do_replace, find_original_update_blocks, find_similar_lines
from .navigator_legacy_prompts import NavigatorLegacyPrompts
from .navigator_prompts import NavigatorPrompts

try:
    import pyperclip
except ImportError:
    pyperclip = None


class NavigatorCoder(Coder):
    """Mode where the LLM autonomously manages which files are in context."""

    edit_format = "navigator"

    # TODO: We'll turn on granular editing by default once those tools stabilize
    use_granular_editing = False

    def __init__(self, *args, **kwargs):
        # Initialize appropriate prompt set before calling parent constructor
        # This needs to happen before super().__init__ so the parent class has access to gpt_prompts
        self.gpt_prompts = (
            NavigatorPrompts() if self.use_granular_editing else NavigatorLegacyPrompts()
        )

        # Dictionary to track recently removed files
        self.recently_removed = {}

        # Tool usage history
        self.tool_usage_history = []
        self.tool_usage_retries = 10
        self.read_tools = {
            "viewfilesatglob",
            "viewfilesmatching",
            "ls",
            "viewfileswithsymbol",
            "grep",
            "listchanges",
            "extractlines",
            "shownumberedcontext",
        }
        self.write_tools = {
            "command",
            "commandinteractive",
            "insertblock",
            "replaceblock",
            "replaceall",
            "replacetext",
            "undochange",
        }

        # Configuration parameters
        self.max_tool_calls = 100  # Maximum number of tool calls per response

        # Context management parameters
        self.large_file_token_threshold = (
            25000  # Files larger than this in tokens are considered large
        )
        self.max_files_per_glob = 50  # Maximum number of files to add at once via glob/grep

        # Enable context management by default only in navigator mode
        self.context_management_enabled = True  # Enabled by default for navigator mode

        # Initialize change tracker for granular editing
        self.change_tracker = ChangeTracker()

        # Track files added during current exploration
        self.files_added_in_exploration = set()

        # Counter for tool calls
        self.tool_call_count = 0

        # Set high max reflections to allow many exploration rounds
        # This controls how many automatic iterations the LLM can do
        self.max_reflections = 15

        # Enable enhanced context blocks by default
        self.use_enhanced_context = True

        # Initialize empty token tracking dictionary and cache structures
        # but don't populate yet to avoid startup delay
        self.context_block_tokens = {}
        self.context_blocks_cache = {}
        self.tokens_calculated = False

        # Track a tool file that failed to load and might need reloading after a fix
        self.tool_file_to_reload_after_fix = None

        # Initialize tool tracking attributes
        self.custom_tools = {}
        self.local_tool_instances = {}

        git_tool_classes = [GitDiff, GitLog, GitShow, GitStatus]
        for tool_class in git_tool_classes:
            instance = tool_class(self)
            self.local_tool_instances[instance.name] = instance

        self.initialize_local_tools()
        super().__init__(*args, **kwargs)

    def initialize_local_tools(self):
        # Ensure self.mcp_tools is always a list
        if not hasattr(self, "mcp_tools") or self.mcp_tools is None:
            self.mcp_tools = []

        # Get the correct set of tool schemas based on the current granular editing mode
        current_local_tool_schemas = self.get_local_tool_schemas()

        # Ensure a LocalServer instance is configured for local tools
        local_server_config = {"name": "local_tools"}
        local_server = LocalServer(local_server_config)

        # Add LocalServer to mcp_servers if not already present
        if not hasattr(self, "mcp_servers") or self.mcp_servers is None:
            self.mcp_servers = []
        if not any(isinstance(s, LocalServer) for s in self.mcp_servers):
            self.mcp_servers.append(local_server)

        # Find the index of the "local_tools" entry within self.mcp_tools
        local_tools_entry_index = -1
        for i, (server_name, _) in enumerate(self.mcp_tools):
            if server_name == "local_tools":
                local_tools_entry_index = i
                break

        # Update or add the "local_tools" entry
        if local_tools_entry_index != -1:
            # Update the existing entry with the new set of tool schemas
            self.mcp_tools[local_tools_entry_index] = ("local_tools", current_local_tool_schemas)
        else:
            # Add a new entry for "local_tools" with the schemas
            self.mcp_tools.append(("local_tools", current_local_tool_schemas))

        # Finally, always call self.functions = self.get_tool_list() to ensure the master list
        # of tools available to the LLM is refreshed.
        self.functions = self.get_tool_list()

    def get_local_tool_schemas(self):
        """Returns the JSON schemas for all local tools."""
        navigation_tools = [
            {
                "type": "function",
                "function": {
                    "name": "ViewFilesAtGlob",
                    "description": "View files matching a glob pattern.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "The glob pattern to match files.",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ViewFilesMatching",
                    "description": "View files containing a specific pattern.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "The pattern to search for in file contents.",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": (
                                    "An optional glob pattern to filter which files are searched."
                                ),
                            },
                            "regex": {
                                "type": "boolean",
                                "description": "Whether the pattern is a regular expression. Defaults to False.",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Ls",
                    "description": "List files in a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "directory": {
                                "type": "string",
                                "description": "The directory to list.",
                            },
                        },
                        "required": ["directory"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "View",
                    "description": "View a specific file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "The path to the file to view.",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Remove",
                    "description": "Remove a file from the chat context.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "The path to the file to remove.",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "MakeEditable",
                    "description": "Make a read-only file editable.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "The path to the file to make editable.",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "MakeReadonly",
                    "description": "Make an editable file read-only.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "The path to the file to make read-only.",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ViewFilesWithSymbol",
                    "description": (
                        "View files that contain a specific symbol (e.g., class, function)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "The symbol to search for.",
                            },
                        },
                        "required": ["symbol"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Command",
                    "description": "Execute a shell command.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_string": {
                                "type": "string",
                                "description": "The shell command to execute.",
                            },
                        },
                        "required": ["command_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "CommandInteractive",
                    "description": "Execute a shell command interactively.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_string": {
                                "type": "string",
                                "description": "The interactive shell command to execute.",
                            },
                        },
                        "required": ["command_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Grep",
                    "description": "Search for a pattern in files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "The pattern to search for.",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": (
                                    "An optional glob pattern to filter which files are searched."
                                ),
                            },
                            "directory": {
                                "type": "string",
                                "description": "Directory to search in. Defaults to '.'.",
                            },
                            "use_regex": {
                                "type": "boolean",
                                "description": "Whether to use regex. Defaults to False.",
                            },
                            "case_insensitive": {
                                "type": "boolean",
                                "description": (
                                    "Whether to perform a case-insensitive search. Defaults to"
                                    " False."
                                ),
                            },
                            "context_before": {
                                "type": "integer",
                                "description": (
                                    "Number of lines to show before a match. Defaults to 5."
                                ),
                            },
                            "context_after": {
                                "type": "integer",
                                "description": (
                                    "Number of lines to show after a match. Defaults to 5."
                                ),
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "CreateTool",
                    "description": (
                        "Create a new custom tool by providing a description and a valid Python"
                        " filename. The new tool will be automatically loaded and available for use."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": (
                                    "A natural language description of the tool to be created. This will be used"
                                    " to generate the tool's Python code."
                                ),
                            },
                            "file_name": {
                                "type": "string",
                                "description": (
                                    "The desired filename for the new tool (e.g., 'my_new_tool.py'). Must end with"
                                    " .py and not contain path separators."
                                ),
                            },
                            "scope": {
                                "type": "string",
                                "enum": ["local", "global"],
                                "description": (
                                    "The scope for the new tool. Can be 'local' (default) for the current project"
                                    " or 'global' for all projects."
                                ),
                            },
                        },
                        "required": ["description", "file_name"],
                    },
                },
            },
        ]

        editing_tools = [
            {
                "type": "function",
                "function": {
                    "name": "ReplaceText",
                    "description": (
                        "Replace specific text. `near_context` (optional) helps find the right spot."
                        " `occurrence` (optional, default 1) specifies which match (-1 for last)."
                        " `dry_run=True` simulates the change."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "find_text": {"type": "string"},
                            "replace_text": {"type": "string"},
                            "near_context": {"type": "string"},
                            "occurrence": {"type": "integer", "default": 1},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "find_text", "replace_text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ReplaceAll",
                    "description": "Replace ALL occurrences of text. Use with caution. `dry_run=True` simulates the change.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "find_text": {"type": "string"},
                            "replace_text": {"type": "string"},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "find_text", "replace_text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "InsertBlock",
                    "description": "Insert a block of code or text. Specify *exactly one* location: `after_pattern`, `before_pattern`, or `position` ('start_of_file' or 'end_of_file'). `occurrence` (optional, default 1) specifies which match (-1 for last). `auto_indent` (optional, default True) adjusts indentation. `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "content": {"type": "string"},
                            "after_pattern": {"type": "string"},
                            "before_pattern": {"type": "string"},
                            "occurrence": {"type": "integer", "default": 1},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                            "position": {"type": "string", "enum": ["top", "bottom"]},
                            "auto_indent": {"type": "boolean", "default": True},
                            "use_regex": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "DeleteBlock",
                    "description": "Delete a block of lines from a file. Specify `start_pattern` and optionally `end_pattern` or `line_count`. Use `near_context` and `occurrence` (optional, default 1, -1 for last) for `start_pattern`. `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "start_pattern": {"type": "string"},
                            "end_pattern": {"type": "string"},
                            "line_count": {"type": "integer"},
                            "near_context": {"type": "string"},
                            "occurrence": {"type": "integer", "default": 1},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "start_pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ReplaceLine",
                    "description": "Replace a specific line number (1-based). `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "line_number": {"type": "integer"},
                            "new_content": {"type": "string"},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "line_number", "new_content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ReplaceLines",
                    "description": "Replace a range of lines (1-based, inclusive). `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "new_content": {"type": "string"},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "start_line", "end_line", "new_content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "IndentLines",
                    "description": (
                        "Indent (`indent_levels` > 0) or unindent (`indent_levels` < 0) a block. Use"
                        " `end_pattern` or `line_count` for range. Use `near_context` and `occurrence`"
                        " (optional, default 1, -1 for last) for `start_pattern`. `dry_run=True`"
                        " simulates."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "start_pattern": {"type": "string"},
                            "end_pattern": {"type": "string"},
                            "line_count": {"type": "integer"},
                            "indent_levels": {"type": "integer", "default": 1},
                            "near_context": {"type": "string"},
                            "occurrence": {"type": "integer", "default": 1},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "start_pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "DeleteLine",
                    "description": "Delete a specific line number (1-based). `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "line_number": {"type": "integer"},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "line_number"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "DeleteLines",
                    "description": "Delete a range of lines (1-based, inclusive). `dry_run=True` simulates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "change_id": {"type": "string"},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["file_path", "start_line", "end_line"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "UndoChange",
                    "description": "Undo a specific change by ID, or the last change made to the specified `file_path`.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "change_id": {"type": "string"},
                            "file_path": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ListChanges",
                    "description": "List recent changes, optionally filtered by `file_path` and limited.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "limit": {"type": "integer", "default": 10},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ExtractLines",
                    "description": (
                        "Extract lines from a source file and append them to a target file."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_file_path": {"type": "string"},
                            "target_file_path": {"type": "string"},
                            "start_pattern": {"type": "string"},
                            "end_pattern": {"type": "string"},
                            "line_count": {"type": "integer"},
                            "near_context": {"type": "string"},
                            "occurrence": {"type": "integer", "default": 1},
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["source_file_path", "target_file_path", "start_pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ShowNumberedContext",
                    "description": (
                        "Show numbered lines of context around a pattern or line number."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "pattern": {"type": "string"},
                            "line_number": {"type": "integer"},
                            "context_lines": {"type": "integer", "default": 3},
                        },
                        "required": ["file_path"],
                    },
                },
            },
        ]

        other_tools = [update_todo_list_schema]

        git_tools = [
            git_diff_schema,
            git_log_schema,
            git_show_schema,
            git_status_schema,
        ]

        all_tools = navigation_tools + other_tools
        if self.use_granular_editing:
            all_tools += editing_tools
        if self.repo:
            all_tools += git_tools

        return all_tools

    def validate_tool_definition(self, tool_definition):
        """
        Validates a tool definition against a schema and checks its parameters.
        """
        # Schema for the overall tool definition structure
        tool_schema = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["function"]},
                "function": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "pattern": r"^[a-zA-Z0-9_]{1,64}$"},
                        "description": {"type": "string"},
                        "parameters": {"type": "object"},  # Will be validated separately as a JSON schema
                    },
                    "required": ["name", "description", "parameters"],
                    "additionalProperties": False,
                },
            },
            "required": ["type", "function"],
            "additionalProperties": False,
        }

        try:
            # Validate the overall structure
            jsonschema.validate(instance=tool_definition, schema=tool_schema)

            # Validate the 'parameters' object as a JSON schema itself
            parameters_schema = tool_definition["function"]["parameters"]
            jsonschema.Draft7Validator.check_schema(parameters_schema)

        except jsonschema.ValidationError as e:
            raise ValueError(f"Invalid tool definition: {e.message} at {e.path}")
        except jsonschema.SchemaError as e:
            raise ValueError(f"Invalid JSON schema in tool parameters: {e.message} at {e.path}")
        except Exception as e:
            raise ValueError(f"Unexpected error during tool definition validation: {e}")

    def _get_global_tools_dir(self):
        """
        Returns the resolved path to the global tools directory (~/.aider.tools).
        """
        return (Path.home() / ".aider.tools").resolve()

    def _get_local_tools_dir(self):
        """
        Returns the resolved path to the local project tools directory (.aider.tools).
        If a git repository is present, it's relative to the repo root.
        Otherwise, it's relative to the current working directory.
        """
        if self.repo and self.repo.root:
            return (Path(self.repo.root) / ".aider.tools").resolve()
        else:
            return (Path.cwd() / ".aider.tools").resolve()

    def tool_add_from_path(self, file_path: str):
        # Safeguard: Only process Python files
        if not file_path.lower().endswith(".py"):
            self.io.tool_error(f"Tool file must be a Python file: {file_path}")
            return

        # Use absolute path for consistency
        abs_file_path = self.abs_root_path(file_path)
        if not os.path.exists(abs_file_path):
            self.io.tool_error(f"Tool file not found: {file_path}")
            return

        try:
            # Dynamically import the module
            spec = importlib.util.spec_from_file_location(
                f"custom_tool_{Path(abs_file_path).stem}", abs_file_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            tool_class = None
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseAiderTool)
                    and obj is not BaseAiderTool
                ):
                    tool_class = obj
                    break

            if tool_class is None:
                raise ValueError(f"No class inheriting from BaseAiderTool found in {file_path}")

            # Instantiate the tool
            tool_instance = tool_class(self)
            tool_name = tool_instance.name
            tool_description = tool_instance.description
            tool_parameters = tool_instance.parameters
            scope = tool_instance.scope

            # Validate the tool definition
            tool_definition = {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_description,
                    "parameters": tool_parameters,
                },
            }
            self.validate_tool_definition(tool_definition)

            # Find the 'local_tools' entry in self.mcp_tools
            local_tools_entry_index = -1
            for i, (server_name, _) in enumerate(self.mcp_tools):
                if server_name == "local_tools":
                    local_tools_entry_index = i
                    break

            if local_tools_entry_index != -1:
                # Get the current list of tools for 'local_tools'
                _, current_local_tools = self.mcp_tools[local_tools_entry_index]
                # Remove any existing tool with the same name
                updated_tools = [
                    t for t in current_local_tools if t["function"]["name"] != tool_name
                ]
                # Add the new/updated tool definition
                updated_tools.append(tool_definition)
                self.mcp_tools[local_tools_entry_index] = ("local_tools", updated_tools)

            # Refresh the master tool list
            self.functions = self.get_tool_list()

            # Store tool metadata
            self.custom_tools[tool_name] = {
                "path": file_path,
                "description": tool_description,
                "scope": scope,
            }

            self.io.tool_output(
                f"Successfully loaded tool: {tool_name} from {self.get_rel_fname(file_path)}"
            )

        except Exception as e:
            rel_file_path = self.get_rel_fname(file_path)
            self.tool_file_to_reload_after_fix = rel_file_path  # Flag the file for potential reload
            self.io.tool_error(f"Error loading tool from {rel_file_path}: {e}")

            # Ask for confirmation before attempting to fix
            if self.io.confirm_ask(
                f"Do you want to try and fix the error in {rel_file_path} now?"
            ):
                self.add_rel_fname(rel_file_path)
                self.io.tool_output(f"Added {rel_file_path} to the chat to be repaired.")
                # ARCHITECT: Added logic to queue the fix prompt
                fix_prompt = (
                    f"The tool at `{rel_file_path}` failed to load with this error:\n\n"
                    f"```\n{e}\n```\n\n"
                    "Please fix the code in the file."
                )
                if self.reflected_message:
                    self.reflected_message += "\n\n" + fix_prompt
                else:
                    self.reflected_message = fix_prompt
            else:
                self.io.tool_output(f"Skipping automatic fix for '{rel_file_path}'.")

    def tool_unload_from_path(self, file_path: str):
        abs_file_path = Path(self.abs_root_path(file_path)).resolve()
        rel_file_path = self.get_rel_fname(str(abs_file_path))

        tools_to_remove = []
        for tool_name, tool_info in self.custom_tools.items():
            if Path(tool_info["path"]).resolve() == abs_file_path:
                tools_to_remove.append(tool_name)

        if not tools_to_remove:
            self.io.tool_error(f"No tool found associated with file path: {rel_file_path}")
            return

        for tool_name in tools_to_remove:
            # Remove from custom_tools and local_tool_instances
            if tool_name in self.custom_tools:
                del self.custom_tools[tool_name]
            if tool_name in self.local_tool_instances:
                del self.local_tool_instances[tool_name]

            # Remove from self.mcp_tools
            local_tools_entry_index = -1
            for i, (server_name, _) in enumerate(self.mcp_tools):
                if server_name == "local_tools":
                    local_tools_entry_index = i
                    break

            if local_tools_entry_index != -1:
                _, current_local_tools = self.mcp_tools[local_tools_entry_index]
                updated_tools = [
                    t for t in current_local_tools if t["function"]["name"] != tool_name
                ]
                self.mcp_tools[local_tools_entry_index] = ("local_tools", updated_tools)

            self.io.tool_output(f"Unloaded tool: {tool_name} from {rel_file_path}")

        # Refresh the master tool list
        self.functions = self.get_tool_list()

    def tool_move(self, file_path: str):
        """Moves a tool between local and global scopes and reloads it."""
        original_abs_path = Path(file_path).resolve()
        original_rel_path = self.get_rel_fname(original_abs_path)

        if not original_abs_path.exists():
            raise FileNotFoundError(f"Tool file not found: {original_rel_path}")
        if not original_abs_path.is_file():
            raise ValueError(f"Path is not a file: {original_rel_path}")
        if original_abs_path.suffix != ".py":
            raise ValueError(f"Not a Python tool file: {original_rel_path}")

        global_tools_dir = self._get_global_tools_dir()
        local_tools_dir = self._get_local_tools_dir()

        is_global = False
        is_local = False

        try:
            if original_abs_path.is_relative_to(global_tools_dir):
                is_global = True
        except ValueError:
            pass

        try:
            if original_abs_path.is_relative_to(local_tools_dir):
                is_local = True
        except ValueError:
            pass

        if not is_global and not is_local:
            raise ValueError(
                f"Tool '{original_rel_path}' is not in a recognized local or global tool directory."
            )

        destination_dir = None
        if is_global:
            destination_dir = local_tools_dir
            self.io.tool_output(f"Moving tool '{original_rel_path}' from global to local scope...")
        elif is_local:
            destination_dir = global_tools_dir
            self.io.tool_output(f"Moving tool '{original_rel_path}' from local to global scope...")

        if destination_dir is None:
            raise ValueError("Could not determine destination directory.")

        destination_path = destination_dir / original_abs_path.name
        destination_rel_path = self.get_rel_fname(destination_path)

        if destination_path.exists():
            raise FileExistsError(
                f"A file named '{destination_rel_path}' already exists at the destination. "
                "Please rename or remove it before moving."
            )

        # 1. Unload the tool from the current session
        self.tool_unload_from_path(str(original_abs_path))

        # 2. Move the file on the filesystem
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(original_abs_path, destination_path)
            self.io.tool_output(f"File moved to: {destination_rel_path}")
        except Exception as e:
            raise IOError(
                f"Failed to move file from {original_rel_path} to {destination_rel_path}: {e}"
            )

        # 3. Load the tool from the new path
        self.tool_add_from_path(str(destination_path))
        self.io.tool_output(f"Reloaded tool '{original_abs_path.name}' with new scope.")

    async def _execute_local_tool_calls(self, tool_calls_list):
        tool_responses = []
        for tool_call in tool_calls_list:
            tool_name = tool_call.function.name
            result_message = ""
            try:
                # Arguments can be a stream of JSON objects.
                # We need to parse them and run a tool call for each.
                args_string = tool_call.function.arguments.strip()
                parsed_args_list = []
                if args_string:
                    json_chunks = utils.split_concatenated_json(args_string)
                    for chunk in json_chunks:
                        try:
                            parsed_args_list.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            self.io.tool_warning(
                                f"Could not parse JSON chunk for tool {tool_name}: {chunk}"
                            )
                            continue

                if not parsed_args_list and not args_string:
                    parsed_args_list.append({})  # For tool calls with no arguments

                all_results_content = []
                tasks = []

                tool_functions = {
                    "viewfilesatglob": execute_view_files_at_glob,
                    "viewfilesmatching": execute_view_files_matching,
                    "ls": execute_ls,
                    "view": execute_view,
                    "remove": _execute_remove,
                    "makeeditable": _execute_make_editable,
                    "makereadonly": _execute_make_readonly,
                    "viewfileswithsymbol": _execute_view_files_with_symbol,
                    "command": _execute_command,
                    "commandinteractive": _execute_command_interactive,
                    "grep": _execute_grep,
                    "replacetext": _execute_replace_text,
                    "replaceall": _execute_replace_all,
                    "insertblock": _execute_insert_block,
                    "deleteblock": _execute_delete_block,
                    "replaceline": _execute_replace_line,
                    "replacelines": _execute_replace_lines,
                    "indentlines": _execute_indent_lines,
                    "deleteline": _execute_delete_line,
                    "deletelines": _execute_delete_lines,
                    "undochange": _execute_undo_change,
                    "listchanges": _execute_list_changes,
                    "extractlines": _execute_extract_lines,
                    "shownumberedcontext": execute_show_numbered_context,
                    "updatetodolist": _execute_update_todo_list,
                }

                for params in parsed_args_list:
                    norm_tool_name = tool_name.lower()

                    if norm_tool_name == "createtool":
                        tool_instance = CreateTool(self)
                        tasks.append(asyncio.to_thread(tool_instance.run, **params))
                        continue

                    func = tool_functions.get(norm_tool_name)

                    if func:
                        if asyncio.iscoroutinefunction(func):
                            tasks.append(func(self, **params))
                        else:
                            tasks.append(asyncio.to_thread(func, self, **params))
                    elif (
                        hasattr(self, "local_tool_instances")
                        and tool_name in self.local_tool_instances
                    ):
                        tool_instance = self.local_tool_instances[tool_name]
                        tasks.append(tool_instance.run(**params))
                    else:
                        all_results_content.append(f"Error: Unknown local tool name '{tool_name}'")

                if tasks:
                    task_results = await asyncio.gather(*tasks)
                    all_results_content.extend(str(res) for res in task_results)

                result_message = "\n\n".join(all_results_content)

            except Exception as e:
                result_message = f"Error executing {tool_name}: {e}"
                self.io.tool_error(
                    f"Error during {tool_name} execution: {e}\n{traceback.format_exc()}"
                )

            tool_responses.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": result_message,
                }
            )
        return tool_responses

    async def _execute_mcp_tool(self, server, tool_name, params):
        """Helper to execute a single MCP tool call, created from legacy format."""
        # Construct a ToolCall object-like structure to be compatible with mcp_client
        function_dict = {"name": tool_name, "arguments": json.dumps(params)}
        tool_call_dict = {
            "id": f"mcp-tool-call-{time.time()}",
            "function": function_dict,
            "type": "function",
        }
        try:
            session = await server.connect()
            call_result = await experimental_mcp_client.call_openai_tool(
                session=session,
                openai_tool=tool_call_dict,
            )

            content_parts = []
            if call_result.content:
                for item in call_result.content:
                    if hasattr(item, "resource"):  # EmbeddedResource
                        resource = item.resource
                        if hasattr(resource, "text"):  # TextResourceContents
                            content_parts.append(resource.text)
                        elif hasattr(resource, "blob"):  # BlobResourceContents
                            try:
                                decoded_blob = base64.b64decode(resource.blob).decode("utf-8")
                                content_parts.append(decoded_blob)
                            except (UnicodeDecodeError, TypeError):
                                name = getattr(resource, "name", "unnamed")
                                mime_type = getattr(resource, "mimeType", "unknown mime type")
                                content_parts.append(
                                    f"[embedded binary resource: {name} ({mime_type})]"
                                )
                    elif hasattr(item, "text"):  # TextContent
                        content_parts.append(item.text)

            return "".join(content_parts)

        except Exception as e:
            self.io.tool_warning(
                f"Executing {tool_name} on {server.name} failed: \n  Error: {e}\n"
            )
            return f"Error executing tool call {tool_name}: {e}"
        finally:
            await server.disconnect()

    def _calculate_context_block_tokens(self, force=False):
        """
        Calculate token counts for all enhanced context blocks.
        This is the central method for calculating token counts,
        ensuring they're consistent across all parts of the code.

        This method populates the cache for context blocks and calculates tokens.

        Args:
            force: If True, recalculate tokens even if already calculated
        """
        # Skip if already calculated and not forced
        if hasattr(self, "tokens_calculated") and self.tokens_calculated and not force:
            return

        # Clear existing token counts
        self.context_block_tokens = {}

        # Initialize the cache for context blocks if needed
        if not hasattr(self, "context_blocks_cache"):
            self.context_blocks_cache = {}

        if not self.use_enhanced_context:
            return

        try:
            # First, clear the cache to force regeneration of all blocks
            self.context_blocks_cache = {}

            # Generate all context blocks and calculate token counts
            block_types = [
                "environment_info",
                "directory_structure",
                "git_status",
                "symbol_outline",
            ]

            for block_type in block_types:
                block_content = self._generate_context_block(block_type)
                if block_content:
                    self.context_block_tokens[block_type] = self.main_model.token_count(
                        block_content
                    )

            # Mark as calculated
            self.tokens_calculated = True
        except Exception:
            # Silently handle errors during calculation
            # This prevents errors in token counting from breaking the main functionality
            pass

    def _generate_context_block(self, block_name):
        """
        Generate a specific context block and cache it.
        This is a helper method for get_cached_context_block.
        """
        content = None

        if block_name == "environment_info":
            content = self.get_environment_info()
        elif block_name == "directory_structure":
            content = self.get_directory_structure()
        elif block_name == "git_status":
            content = self.get_git_status()
        elif block_name == "symbol_outline":
            content = self.get_context_symbol_outline()
        elif block_name == "context_summary":
            content = self.get_context_summary()
        elif block_name == "todo_list":
            content = self.get_todo_list()

        # Cache the result if it's not None
        if content is not None:
            self.context_blocks_cache[block_name] = content

        return content

    def get_cached_context_block(self, block_name):
        """
        Get a context block from the cache, or generate it if not available.
        This should be used by format_chat_chunks to avoid regenerating blocks.

        This will ensure tokens are calculated if they haven't been yet.
        """
        # Make sure tokens have been calculated at least once
        if not hasattr(self, "tokens_calculated") or not self.tokens_calculated:
            self._calculate_context_block_tokens()

        # Return from cache if available
        if hasattr(self, "context_blocks_cache") and block_name in self.context_blocks_cache:
            return self.context_blocks_cache[block_name]

        # Otherwise generate and cache the block
        return self._generate_context_block(block_name)

    def set_granular_editing(self, enabled):
        """
        Switch between granular editing tools and legacy search/replace.

        Args:
            enabled (bool): True to use granular editing tools, False to use legacy search/replace
        """
        self.use_granular_editing = enabled
        self.gpt_prompts = NavigatorPrompts() if enabled else NavigatorLegacyPrompts()
        # Re-initialize local tools to load the correct set based on the new flag
        self.initialize_local_tools()

    def get_context_symbol_outline(self):
        """
        Generate a symbol outline for files currently in context using Tree-sitter,
        bypassing the cache for freshness.
        """
        if not self.use_enhanced_context or not self.repo_map:
            return None

        try:
            result = '<context name="symbol_outline">\n'
            result += "## Symbol Outline (Current Context)\n\n"
            result += (
                "Code definitions (classes, functions, methods, etc.) found in files currently in"
                " chat context.\n\n"
            )

            files_to_outline = list(self.abs_fnames) + list(self.abs_read_only_fnames)
            if not files_to_outline:
                result += "No files currently in context.\n"
                result += "</context>"
                return result

            all_tags_by_file = defaultdict(list)
            has_symbols = False

            # Use repo_map which should be initialized in BaseCoder
            if not self.repo_map:
                self.io.tool_warning("RepoMap not initialized, cannot generate symbol outline.")
                return None  # Or return a message indicating repo map is unavailable

            for abs_fname in sorted(files_to_outline):
                rel_fname = self.get_rel_fname(abs_fname)
                try:
                    # Call get_tags_raw directly to bypass cache and ensure freshness
                    tags = list(self.repo_map.get_tags_raw(abs_fname, rel_fname))
                    if tags:
                        all_tags_by_file[rel_fname].extend(tags)
                        has_symbols = True
                except Exception as e:
                    self.io.tool_warning(f"Could not get symbols for {rel_fname}: {e}")

            if not has_symbols:
                result += "No symbols found in the current context files.\n"
            else:
                for rel_fname in sorted(all_tags_by_file.keys()):
                    tags = sorted(all_tags_by_file[rel_fname], key=lambda t: (t.line, t.name))

                    definition_tags = []
                    for tag in tags:
                        # Use specific_kind first if available, otherwise fall back to kind
                        kind_to_check = tag.specific_kind or tag.kind
                        # Check if the kind represents a definition using the set from RepoMap
                        if (
                            kind_to_check
                            and kind_to_check.lower() in self.repo_map.definition_kinds
                        ):
                            definition_tags.append(tag)

                    if definition_tags:
                        result += f"### {rel_fname}\n"
                        # Simple list format for now, could be enhanced later (e.g., indentation for scope)
                        for tag in definition_tags:
                            # Display line number if available
                            line_info = f", line {tag.line + 1}" if tag.line >= 0 else ""
                            # Display the specific kind (which we checked)
                            kind_to_check = tag.specific_kind or tag.kind  # Recalculate for safety
                            result += f"- {tag.name} ({kind_to_check}{line_info})\n"
                        result += "\n"  # Add space between files

            result += "</context>"
            return result.strip()  # Remove trailing newline if any

        except Exception as e:
            self.io.tool_error(f"Error generating symbol outline: {str(e)}")
            # Optionally include traceback for debugging if verbose
            # if self.verbose:
            #     self.io.tool_error(traceback.format_exc())
            return None

    def format_chat_chunks(self):
        """
        Override parent's format_chat_chunks to include enhanced context blocks with a
        cleaner, more hierarchical structure for better organization.

        Optimized for prompt caching by placing context blocks strategically:
        1. Relatively static blocks (directory structure, environment info) before done_messages
        2. Dynamic blocks (context summary, symbol outline, git status) after chat_files

        This approach preserves prefix caching while providing fresh context information.
        """
        # First get the normal chat chunks from the parent method without calling super
        # We'll manually build the chunks to control placement of context blocks
        chunks = self.format_chat_chunks_base()

        # If enhanced context blocks are not enabled, just return the base chunks
        if not self.use_enhanced_context:
            return chunks

        # Make sure token counts are updated - using centralized method
        # This also populates the context block cache
        self._calculate_context_block_tokens()

        # Get blocks from cache to avoid regenerating them
        env_context = self.get_cached_context_block("environment_info")
        dir_structure = self.get_cached_context_block("directory_structure")
        git_status = self.get_cached_context_block("git_status")
        symbol_outline = self.get_cached_context_block("symbol_outline")
        todo_list = self.get_cached_context_block("todo_list")

        # Context summary needs special handling because it depends on other blocks
        context_summary = self.get_context_summary()

        # 1. Add relatively static blocks BEFORE done_messages
        # These blocks change less frequently and can be part of the cacheable prefix
        static_blocks = []
        if dir_structure:
            static_blocks.append(dir_structure)
        if env_context:
            static_blocks.append(env_context)

        if static_blocks:
            static_message = "\n\n".join(static_blocks)
            # Insert as a system message right before done_messages
            chunks.done.insert(0, dict(role="system", content=static_message))

        # 2. Add dynamic blocks AFTER chat_files
        # These blocks change with the current files in context
        dynamic_blocks = []
        if todo_list:
            dynamic_blocks.append(todo_list)
        if context_summary:
            dynamic_blocks.append(context_summary)
        if symbol_outline:
            dynamic_blocks.append(symbol_outline)
        if git_status:
            dynamic_blocks.append(git_status)

        # Add tool usage context if there are repetitive tools
        if hasattr(self, "tool_usage_history") and self.tool_usage_history:
            repetitive_tools = self._get_repetitive_tools()
            if repetitive_tools:
                tool_context = self._generate_tool_context(repetitive_tools)
                if tool_context:
                    dynamic_blocks.append(tool_context)

        if dynamic_blocks:
            dynamic_message = "\n\n".join(dynamic_blocks)
            # Append as a system message after chat_files
            chunks.chat_files.append(dict(role="system", content=dynamic_message))

        return chunks

    def format_chat_chunks_base(self):
        """
        Create base chat chunks without enhanced context blocks.
        This is a copy of the parent's format_chat_chunks method to avoid
        calling super() which would create a recursive loop.
        """
        self.choose_fence()
        main_sys = self.fmt_system_prompt(self.gpt_prompts.main_system)

        example_messages = []
        if self.main_model.examples_as_sys_msg:
            if self.gpt_prompts.example_messages:
                main_sys += "\n# Example conversations:\n\n"
            for msg in self.gpt_prompts.example_messages:
                role = msg["role"]
                content = self.fmt_system_prompt(msg["content"])
                main_sys += f"## {role.upper()}: {content}\n\n"
            main_sys = main_sys.strip()
        else:
            for msg in self.gpt_prompts.example_messages:
                example_messages.append(
                    dict(
                        role=msg["role"],
                        content=self.fmt_system_prompt(msg["content"]),
                    )
                )
            if self.gpt_prompts.example_messages:
                example_messages += [
                    dict(
                        role="user",
                        content=(
                            "I switched to a new code base. Please don't consider the above files"
                            " or try to edit them any longer."
                        ),
                    ),
                    dict(role="assistant", content="Ok."),
                ]

        if self.gpt_prompts.system_reminder:
            main_sys += "\n" + self.fmt_system_prompt(self.gpt_prompts.system_reminder)

        chunks = ChatChunks()

        if self.main_model.use_system_prompt:
            chunks.system = [
                dict(role="system", content=main_sys),
            ]
        else:
            chunks.system = [
                dict(role="user", content=main_sys),
                dict(role="assistant", content="Ok."),
            ]

        chunks.examples = example_messages

        self.summarize_end()
        chunks.done = self.done_messages

        chunks.repo = self.get_repo_messages()
        chunks.readonly_files = self.get_readonly_files_messages()
        chunks.chat_files = self.get_chat_files_messages()

        if self.gpt_prompts.system_reminder:
            reminder_message = [
                dict(
                    role="system", content=self.fmt_system_prompt(self.gpt_prompts.system_reminder)
                ),
            ]
        else:
            reminder_message = []

        chunks.cur = list(self.cur_messages)
        chunks.reminder = []

        # Use accurate token counting method that considers enhanced context blocks
        base_messages = chunks.all_messages()
        messages_tokens = self.main_model.token_count(base_messages)
        reminder_tokens = self.main_model.token_count(reminder_message)
        cur_tokens = self.main_model.token_count(chunks.cur)

        if None not in (messages_tokens, reminder_tokens, cur_tokens):
            total_tokens = messages_tokens
            # Only add tokens for reminder and cur if they're not already included
            # in the messages_tokens calculation
            if not chunks.reminder:
                total_tokens += reminder_tokens
            if not chunks.cur:
                total_tokens += cur_tokens
        else:
            # add the reminder anyway
            total_tokens = 0

        if chunks.cur:
            final = chunks.cur[-1]
        else:
            final = None

        max_input_tokens = self.main_model.info.get("max_input_tokens") or 0
        # Add the reminder prompt if we still have room to include it.
        if (
            not max_input_tokens
            or total_tokens < max_input_tokens
            and self.gpt_prompts.system_reminder
        ):
            if self.main_model.reminder == "sys":
                chunks.reminder = reminder_message
            elif self.main_model.reminder == "user" and final and final["role"] == "user":
                # stuff it into the user message
                new_content = (
                    final["content"]
                    + "\n\n"
                    + self.fmt_system_prompt(self.gpt_prompts.system_reminder)
                )
                chunks.cur[-1] = dict(role=final["role"], content=new_content)

        return chunks

    def get_context_summary(self):
        """
        Generate a summary of the current context, including file content tokens and additional context blocks,
        with an accurate total token count.
        """
        if not self.use_enhanced_context:
            return None

        # If context_summary is already in the cache, return it
        if (
            hasattr(self, "context_blocks_cache")
            and "context_summary" in self.context_blocks_cache
        ):
            return self.context_blocks_cache["context_summary"]

        try:
            # Make sure token counts are updated before generating the summary
            if not hasattr(self, "context_block_tokens") or not self.context_block_tokens:
                self._calculate_context_block_tokens()

            result = '<context name="context_summary">\n'
            result += "## Current Context Overview\n\n"
            max_input_tokens = self.main_model.info.get("max_input_tokens") or 0
            if max_input_tokens:
                result += f"Model context limit: {max_input_tokens:,} tokens\n\n"

            total_file_tokens = 0
            editable_tokens = 0
            readonly_tokens = 0
            editable_files = []
            readonly_files = []

            # Editable files
            if self.abs_fnames:
                result += "### Editable Files\n\n"
                for fname in sorted(self.abs_fnames):
                    rel_fname = self.get_rel_fname(fname)
                    content = self.io.read_text(fname)
                    if content is not None:
                        tokens = self.main_model.token_count(content)
                        total_file_tokens += tokens
                        editable_tokens += tokens
                        size_indicator = (
                            "🔴 Large"
                            if tokens > 5000
                            else ("🟡 Medium" if tokens > 1000 else "🟢 Small")
                        )
                        editable_files.append(
                            f"- {rel_fname}: {tokens:,} tokens ({size_indicator})"
                        )
                if editable_files:
                    result += "\n".join(editable_files) + "\n\n"
                    result += (
                        f"**Total editable: {len(editable_files)} files,"
                        f" {editable_tokens:,} tokens**\n\n"
                    )
                else:
                    result += "No editable files in context\n\n"

            # Read-only files
            if self.abs_read_only_fnames:
                result += "### Read-Only Files\n\n"
                for fname in sorted(self.abs_read_only_fnames):
                    rel_fname = self.get_rel_fname(fname)
                    content = self.io.read_text(fname)
                    if content is not None:
                        tokens = self.main_model.token_count(content)
                        total_file_tokens += tokens
                        readonly_tokens += tokens
                        size_indicator = (
                            "🔴 Large"
                            if tokens > 5000
                            else ("🟡 Medium" if tokens > 1000 else "🟢 Small")
                        )
                        readonly_files.append(
                            f"- {rel_fname}: {tokens:,} tokens ({size_indicator})"
                        )
                if readonly_files:
                    result += "\n".join(readonly_files) + "\n\n"
                    result += (
                        f"**Total read-only: {len(readonly_files)} files,"
                        f" {readonly_tokens:,} tokens**\n\n"
                    )
                else:
                    result += "No read-only files in context\n\n"

            # Use the pre-calculated context block tokens
            extra_tokens = sum(self.context_block_tokens.values())
            total_tokens = total_file_tokens + extra_tokens

            result += f"**Total files usage: {total_file_tokens:,} tokens**\n\n"
            result += f"**Additional context usage: {extra_tokens:,} tokens**\n\n"
            result += f"**Total context usage: {total_tokens:,} tokens**"
            if max_input_tokens:
                percentage = (total_tokens / max_input_tokens) * 100
                result += f" ({percentage:.1f}% of limit)"
                if percentage > 80:
                    result += "\n\n⚠️ **Context is getting full!** Remove non-essential files via:\n"
                    result += '- `[tool_call(Remove, file_path="path/to/large_file.ext")]`\n'
                    result += "- Keep only essential files in context for best performance"
            result += "\n</context>"

            # Cache the result
            if not hasattr(self, "context_blocks_cache"):
                self.context_blocks_cache = {}
            self.context_blocks_cache["context_summary"] = result

            return result
        except Exception as e:
            self.io.tool_error(f"Error generating context summary: {str(e)}")
            return None

    def get_environment_info(self):
        """
        Generate an environment information context block with key system details.
        Returns formatted string with working directory, platform, date, and other relevant environment details.
        """
        if not self.use_enhanced_context:
            return None

        try:
            # Get current date in ISO format
            current_date = datetime.now().strftime("%Y-%m-%d")

            # Get platform information
            platform_info = platform.platform()

            # Get language preference
            language = self.chat_language or locale.getlocale()[0] or "en-US"

            result = '<context name="environment_info">\n'
            result += "## Environment Information\n\n"
            result += f"- Working directory: {self.root}\n"
            result += f"- Current date: {current_date}\n"
            result += f"- Platform: {platform_info}\n"
            result += f"- Language preference: {language}\n"

            # Add git repo information if available
            if self.repo:
                try:
                    rel_repo_dir = self.repo.get_rel_repo_dir()
                    num_files = len(self.repo.get_tracked_files())
                    result += f"- Git repository: {rel_repo_dir} with {num_files:,} files\n"
                except Exception:
                    result += "- Git repository: active but details unavailable\n"
            else:
                result += "- Git repository: none\n"

            # Add enabled features information
            features = []
            if self.context_management_enabled:
                features.append("context management")
            if self.use_enhanced_context:
                features.append("enhanced context blocks")
            if features:
                result += f"- Enabled features: {', '.join(features)}\n"

            result += "</context>"
            return result
        except Exception as e:
            self.io.tool_error(f"Error generating environment info: {str(e)}")
            return None

    async def process_tool_calls(self, tool_call_response):
        """
        Track tool usage before calling the base implementation.
        """

        if self.partial_response_tool_calls:
            for tool_call in self.partial_response_tool_calls:
                self.tool_usage_history.append(tool_call.get("function", {}).get("name"))

        if len(self.tool_usage_history) > self.tool_usage_retries:
            self.tool_usage_history.pop(0)

        return await super().process_tool_calls(tool_call_response)

    async def reply_completed(self):
        """Process the completed response from the LLM.

        This is a key method that:
        1. Processes any tool commands in the response (only after a '---' line)
        2. Processes any SEARCH/REPLACE blocks in the response (only before the '---' line if one exists)
        3. If tool commands were found, sets up for another automatic round

        This enables the "auto-exploration" workflow where the LLM can
        iteratively discover and analyze relevant files before providing
        a final answer to the user's question.
        """
        # In granular editing mode, tool calls are handled by BaseCoder's process_tool_calls.
        # This method is now only for legacy tool call format and search/replace blocks.
        if self.use_granular_editing:
            # Handle SEARCH/REPLACE blocks
            content = self.partial_response_content
            if not content or not content.strip():
                return True

            # Check for search/replace blocks
            has_search = "<<<<<<< SEARCH" in content
            has_divider = "=======" in content
            has_replace = ">>>>>>> REPLACE" in content
            if has_search and has_divider and has_replace:
                self.io.tool_output("Detected edit blocks, applying changes...")
                edited_files = await self._apply_edits_from_response()
                if self.reflected_message:
                    return False  # Trigger reflection if edits failed

                # If edits were successful, we might want to reflect.
                # For now, let's consider the turn complete.

            # Since tool calls are handled earlier, we finalize the turn.
            self.tool_call_count = 0
            self.files_added_in_exploration = set()
            self.move_back_cur_messages(None)
            return True

        # Legacy tool call processing for use_granular_editing=False
        content = self.partial_response_content
        if not content or not content.strip():
            if len(self.tool_usage_history) > self.tool_usage_retries:
                self.tool_usage_history = []
            return True
        original_content = content  # Keep the original response

        # Process tool commands: returns content with tool calls removed, results, flag if any tool calls were found,
        # and the content before the last '---' line
        (
            processed_content,
            result_messages,
            tool_calls_found,
            content_before_last_separator,
            tool_names_this_turn,
        ) = await self._process_tool_commands(content)

        # Since we are no longer suppressing, the partial_response_content IS the final content.
        # We might want to update it to the processed_content (without tool calls) if we don't
        # want the raw tool calls to remain in the final assistant message history.
        # Let's update it for cleaner history.
        self.partial_response_content = processed_content.strip()

        # Process implicit file mentions using the content *after* tool calls were removed
        self._process_file_mentions(processed_content)

        # Check if the content contains the SEARCH/REPLACE markers
        has_search = "<<<<<<< SEARCH" in self.partial_response_content
        has_divider = "=======" in self.partial_response_content
        has_replace = ">>>>>>> REPLACE" in self.partial_response_content
        edit_match = has_search and has_divider and has_replace

        # Check if there's a '---' line - if yes, SEARCH/REPLACE blocks can only appear before it
        separator_marker = "\n---\n"
        if separator_marker in original_content and edit_match:
            # Check if the edit blocks are only in the part before the last '---' line
            has_search_before = "<<<<<<< SEARCH" in content_before_last_separator
            has_divider_before = "=======" in content_before_last_separator
            has_replace_before = ">>>>>>> REPLACE" in content_before_last_separator
            edit_match = has_search_before and has_divider_before and has_replace_before

        if edit_match:
            self.io.tool_output("Detected edit blocks, applying changes within Navigator...")
            edited_files = await self._apply_edits_from_response()
            # If _apply_edits_from_response set a reflected_message (due to errors),
            # return False to trigger a reflection loop.
            if self.reflected_message:
                return False

            # If edits were successfully applied and we haven't exceeded reflection limits,
            # set up for another iteration (similar to tool calls)
            if edited_files and self.num_reflections < self.max_reflections:
                # Get the original user question from the most recent user message
                if self.cur_messages and len(self.cur_messages) >= 1:
                    for msg in reversed(self.cur_messages):
                        if msg["role"] == "user":
                            original_question = msg["content"]
                            break
                    else:
                        # Default if no user message found
                        original_question = (
                            "Please continue your exploration and provide a final answer."
                        )

                    # Construct the message for the next turn
                    next_prompt = (
                        "I have applied the edits you suggested. "
                        f"The following files were modified: {', '.join(edited_files)}. "
                        "Let me continue working on your request.\n\n"
                        f"Your original question was: {original_question}"
                    )

                    self.reflected_message = next_prompt
                    self.io.tool_output("Continuing after applying edits...")
                    return False  # Indicate that we need another iteration

        # If any tool calls were found and we haven't exceeded reflection limits, set up for another iteration
        # This is implicit continuation when any tool calls are present, rather than requiring Continue explicitly
        if tool_calls_found and self.num_reflections < self.max_reflections:
            # Reset tool counter for next iteration
            self.tool_call_count = 0
            # Clear exploration files for the next round
            self.files_added_in_exploration = set()

            # Get the original user question from the most recent user message
            if self.cur_messages and len(self.cur_messages) >= 1:
                for msg in reversed(self.cur_messages):
                    if msg["role"] == "user":
                        original_question = msg["content"]
                        break
                else:
                    # Default if no user message found
                    original_question = (
                        "Please continue your exploration and provide a final answer."
                    )

                # Construct the message for the next turn, including tool results
                next_prompt_parts = []
                next_prompt_parts.append(
                    "I have processed the results of the previous tool calls. "
                    "Let me analyze them and continue working towards your request."
                )

                if result_messages:
                    next_prompt_parts.append("\nResults from previous tool calls:")
                    # result_messages already have [Result (...): ...] format
                    next_prompt_parts.extend(result_messages)
                    next_prompt_parts.append(
                        "\nBased on these results and the updated file context, I will proceed."
                    )
                else:
                    next_prompt_parts.append(
                        "\nNo specific results were returned from the previous tool calls, but the"
                        " file context may have been updated. I will proceed based on the current"
                        " context."
                    )

                next_prompt_parts.append(f"\nYour original question was: {original_question}")

                self.reflected_message = "\n".join(next_prompt_parts)

                self.io.tool_output("Continuing exploration...")
                return False  # Indicate that we need another iteration
        else:
            # Exploration finished for this turn.
            # Append results to the content that will be stored in history.
            if result_messages:
                results_block = "\n\n" + "\n".join(result_messages)
                # Append results to the cleaned content
                self.partial_response_content += results_block

        # After applying edits OR determining no edits were needed (and no reflection needed),
        # the turn is complete. Reset counters and finalize history.
        self.tool_call_count = 0
        self.files_added_in_exploration = set()
        # Move cur_messages to done_messages
        self.move_back_cur_messages(
            None
        )  # Pass None as we handled commit message earlier if needed
        return True  # Indicate exploration is finished for this round

    def post_edit_actions(self):
        """
        Hook for actions to be performed after edits are applied in a turn.
        This implementation checks if a failed tool file was fixed and prompts for reload.
        """
        if self.tool_file_to_reload_after_fix:
            rel_file_path = self.tool_file_to_reload_after_fix
            if rel_file_path in self.aider_edited_files:
                # The AI has just edited the tool file that previously failed to load.
                # Prompt the user to reload it.
                self.tool_file_to_reload_after_fix = None  # Reset to prevent re-prompting
                abs_file_path = self.abs_root_path(rel_file_path)

                if self.io.confirm_ask(
                    f"The tool file '{rel_file_path}' was just edited. Do you want to reload it?",
                    subject="Tool file updated",
                ):
                    self.io.tool_output(f"Reloading tool from '{rel_file_path}'...")
                    self.tool_add_from_path(abs_file_path)
                else:
                    self.io.tool_output(f"Skipping reload of tool from '{rel_file_path}'.")

        super().post_edit_actions()

    async def _process_tool_commands(self, content):
        """
        Process tool commands in the `[tool_call(name, param=value)]` format within the content.

        Rules:
        1. Tool calls must appear after the LAST '---' line separator in the content
        2. Any tool calls before this last separator are treated as text (not executed)
        3. SEARCH/REPLACE blocks can only appear before this last separator

        Returns processed content, result messages, and a flag indicating if any tool calls were found.
        Also returns the content before the last separator for SEARCH/REPLACE block validation.
        """
        result_messages = []
        modified_content = content  # Start with original content
        tool_calls_found = False
        call_count = 0
        max_calls = self.max_tool_calls
        tool_names = []

        # Check if there's a '---' separator and only process tool calls after the LAST one
        separator_marker = "---"
        content_parts = content.split(separator_marker)

        # If there's no separator, treat the entire content as before the separator
        if len(content_parts) == 1:
            # Return the original content with no tool calls processed, and the content itself as before_separator
            return content, result_messages, False, content, tool_names

        # Take everything before the last separator (including intermediate separators)
        content_before_separator = separator_marker.join(content_parts[:-1])
        # Take only what comes after the last separator
        content_after_separator = content_parts[-1]

        # Find tool calls using a more robust method, but only in the content after separator
        processed_content = content_before_separator + separator_marker
        last_index = 0

        # Support any [tool_...(...)] format
        tool_call_pattern = re.compile(r"\[tool_.*?\(", re.DOTALL)
        end_marker = "]"  # The parenthesis balancing finds the ')', we just need the final ']'

        while True:
            match = tool_call_pattern.search(content_after_separator, last_index)
            if not match:
                processed_content += content_after_separator[last_index:]
                break

            start_pos = match.start()
            start_marker = match.group(0)

            # Check for escaped tool call: \[tool_...
            # Count preceding backslashes to handle \\
            backslashes = 0
            p = start_pos - 1
            while p >= 0 and content_after_separator[p] == "\\":
                backslashes += 1
                p -= 1

            if backslashes % 2 == 1:
                # Odd number of backslashes means it's escaped. Treat as text.
                # We append up to the end of the marker and continue searching.
                processed_content += content_after_separator[
                    last_index : start_pos + len(start_marker)
                ]
                last_index = start_pos + len(start_marker)
                continue

            # Append content before the (non-escaped) tool call
            processed_content += content_after_separator[last_index:start_pos]

            scan_start_pos = start_pos + len(start_marker)
            paren_level = 1
            in_single_quotes = False
            in_double_quotes = False
            escaped = False
            end_paren_pos = -1

            # Scan to find the matching closing parenthesis, respecting quotes
            for i in range(scan_start_pos, len(content_after_separator)):
                char = content_after_separator[i]

                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "'" and not in_double_quotes:
                    in_single_quotes = not in_single_quotes
                elif char == '"' and not in_single_quotes:
                    in_double_quotes = not in_double_quotes
                elif char == "(" and not in_single_quotes and not in_double_quotes:
                    paren_level += 1
                elif char == ")" and not in_single_quotes and not in_double_quotes:
                    paren_level -= 1
                    if paren_level == 0:
                        end_paren_pos = i
                        break

            # Check for the end marker after the closing parenthesis, skipping whitespace
            expected_end_marker_start = end_paren_pos + 1
            actual_end_marker_start = -1
            end_marker_found = False
            if end_paren_pos != -1:  # Only search if we found a closing parenthesis
                for j in range(expected_end_marker_start, len(content_after_separator)):
                    if not content_after_separator[j].isspace():
                        actual_end_marker_start = j
                        # Check if the found character is the end marker ']'
                        if content_after_separator[actual_end_marker_start] == end_marker:
                            end_marker_found = True
                        break  # Stop searching after first non-whitespace char

            if not end_marker_found:
                # Try to extract the tool name for better error message
                tool_name = "unknown"
                try:
                    # Look for the first comma after the tool call start
                    partial_content = content_after_separator[
                        scan_start_pos : scan_start_pos + 100
                    ]  # Limit to avoid huge strings
                    comma_pos = partial_content.find(",")
                    if comma_pos > 0:
                        tool_name = partial_content[:comma_pos].strip()
                    else:
                        # If no comma, look for opening parenthesis or first whitespace
                        space_pos = partial_content.find(" ")
                        paren_pos = partial_content.find("(")
                        if space_pos > 0 and (paren_pos < 0 or space_pos < paren_pos):
                            tool_name = partial_content[:space_pos].strip()
                        elif paren_pos > 0:
                            tool_name = partial_content[:paren_pos].strip()
                except Exception:
                    pass  # Silently fail if we can't extract the name

                # Malformed call: couldn't find matching ')' or the subsequent ']'
                self.io.tool_warning(
                    f"Malformed tool call for '{tool_name}'. Missing closing parenthesis or"
                    " bracket. Skipping."
                )
                # Append the start marker itself to processed_content so it's not lost
                processed_content += start_marker
                last_index = scan_start_pos  # Continue searching after the marker
                continue

            # Found a potential tool call
            # Adjust full_match_str and last_index based on the actual end marker ']' position
            full_match_str = content_after_separator[
                start_pos : actual_end_marker_start + 1
            ]  # End marker ']' is 1 char
            inner_content = content_after_separator[scan_start_pos:end_paren_pos].strip()
            last_index = (
                actual_end_marker_start + 1
            )  # Move past the processed call (including ']')

            call_count += 1
            if call_count > max_calls:
                self.io.tool_warning(
                    f"Exceeded maximum tool calls ({max_calls}). Skipping remaining calls."
                )
                # Don't append the skipped call to processed_content
                continue  # Skip processing this call

            tool_calls_found = True
            tool_name = None
            params = {}
            result_message = None

            # Mark that we found at least one tool call (assuming it passes validation)
            tool_calls_found = True

            try:
                # Pre-process inner_content to handle non-identifier tool names by quoting them.
                # This allows ast.parse to succeed on names like 'resolve-library-id'.
                if inner_content:
                    parts = inner_content.split(",", 1)
                    potential_tool_name = parts[0].strip()

                    is_string = (
                        potential_tool_name.startswith("'") and potential_tool_name.endswith("'")
                    ) or (
                        potential_tool_name.startswith('"') and potential_tool_name.endswith('"')
                    )

                    if not potential_tool_name.isidentifier() and not is_string:
                        # It's not a valid identifier and not a string, so quote it.
                        # Use json.dumps to handle escaping correctly.
                        quoted_tool_name = json.dumps(potential_tool_name)
                        if len(parts) > 1:
                            inner_content = quoted_tool_name + ", " + parts[1]
                        else:
                            inner_content = quoted_tool_name

                # Wrap the inner content to make it parseable as a function call
                # Example: ToolName, key="value" becomes f(ToolName, key="value")
                parse_str = f"f({inner_content})"
                parsed_ast = ast.parse(parse_str)

                # Validate AST structure
                if (
                    not isinstance(parsed_ast, ast.Module)
                    or not parsed_ast.body
                    or not isinstance(parsed_ast.body[0], ast.Expr)
                ):
                    raise ValueError("Unexpected AST structure")
                call_node = parsed_ast.body[0].value
                if not isinstance(call_node, ast.Call):
                    raise ValueError("Expected a Call node")

                # Extract tool name (should be the first positional argument)
                if not call_node.args:
                    raise ValueError("Tool name not found or invalid")

                tool_name_node = call_node.args[0]
                if isinstance(tool_name_node, ast.Name):
                    tool_name = tool_name_node.id
                elif isinstance(tool_name_node, ast.Constant) and isinstance(
                    tool_name_node.value, str
                ):
                    tool_name = tool_name_node.value
                else:
                    raise ValueError("Tool name must be an identifier or a string literal")

                tool_names.append(tool_name)

                # Extract keyword arguments
                for keyword in call_node.keywords:
                    key = keyword.arg
                    value_node = keyword.value
                    # Extract value based on AST node type
                    if isinstance(value_node, ast.Constant):
                        value = value_node.value
                        # Check if this is a multiline string and trim whitespace
                        if isinstance(value, str) and "\n" in value:
                            # Get the source line(s) for this node to check if it's a triple-quoted string
                            lineno = value_node.lineno if hasattr(value_node, "lineno") else 0
                            end_lineno = (
                                value_node.end_lineno
                                if hasattr(value_node, "end_lineno")
                                else lineno
                            )
                            if end_lineno > lineno:  # It's a multiline string
                                # Trim exactly one leading and one trailing newline if present
                                if value.startswith("\n"):
                                    value = value[1:]
                                if value.endswith("\n"):
                                    value = value[:-1]
                    elif isinstance(
                        value_node, ast.Name
                    ):  # Handle unquoted values like True/False/None or variables
                        id_val = value_node.id.lower()
                        if id_val == "true":
                            value = True
                        elif id_val == "false":
                            value = False
                        elif id_val == "none":
                            value = None
                        else:
                            value = value_node.id  # Keep as string if it's something else
                    # Add more types if needed (e.g., ast.List, ast.Dict)
                    else:
                        # Attempt to reconstruct the source for complex types, or raise error
                        try:
                            # Note: ast.unparse requires Python 3.9+
                            # If using older Python, might need a different approach or limit supported types
                            value = ast.unparse(value_node)
                        except AttributeError:  # Handle case where ast.unparse is not available
                            raise ValueError(
                                f"Unsupported argument type for key '{key}': {type(value_node)}"
                            )
                        except Exception as unparse_e:
                            raise ValueError(
                                f"Could not unparse value for key '{key}': {unparse_e}"
                            )

                    # Check for suppressed values (e.g., "...")
                    suppressed_arg_values = ["..."]
                    if isinstance(value, str) and value in suppressed_arg_values:
                        self.io.tool_warning(
                            f"Skipping suppressed argument value '{value}' for key '{key}' in tool"
                            f" '{tool_name}'"
                        )
                        continue

                    params[key] = value

            except (SyntaxError, ValueError) as e:
                result_message = f"Error parsing tool call '{inner_content}': {e}"
                self.io.tool_error(f"Failed to parse tool call: {full_match_str}\nError: {e}")
                # Don't append the malformed call to processed_content
                result_messages.append(f"[Result (Parse Error): {result_message}]")
                continue  # Skip execution
            except Exception as e:  # Catch any other unexpected parsing errors
                result_message = f"Unexpected error parsing tool call '{inner_content}': {e}"
                self.io.tool_error(
                    f"Unexpected error during parsing: {full_match_str}\nError:"
                    f" {e}\n{traceback.format_exc()}"
                )
                result_messages.append(f"[Result (Parse Error): {result_message}]")
                continue

            # Execute the tool based on its name
            try:
                # Normalize tool name for case-insensitive matching
                norm_tool_name = tool_name.lower()

                if norm_tool_name == "viewfilesatglob":
                    result_message = await asyncio.to_thread(
                        execute_view_files_at_glob, self, **params
                    )
                elif norm_tool_name == "viewfilesmatching":
                    result_message = await asyncio.to_thread(
                        execute_view_files_matching, self, **params
                    )
                elif norm_tool_name == "ls":
                    result_message = await asyncio.to_thread(execute_ls, self, **params)
                elif norm_tool_name == "view":
                    result_message = await asyncio.to_thread(execute_view, self, **params)
                elif norm_tool_name == "remove":
                    result_message = await asyncio.to_thread(_execute_remove, self, **params)
                elif norm_tool_name == "makeeditable":
                    result_message = await asyncio.to_thread(
                        _execute_make_editable, self, **params
                    )
                elif norm_tool_name == "makereadonly":
                    result_message = await asyncio.to_thread(
                        _execute_make_readonly, self, **params
                    )
                elif norm_tool_name == "viewfileswithsymbol":
                    result_message = await asyncio.to_thread(
                        _execute_view_files_with_symbol, self, **params
                    )
                elif norm_tool_name == "command":
                    result_message = await _execute_command(self, **params)
                elif norm_tool_name == "commandinteractive":
                    result_message = await _execute_command_interactive(self, **params)
                elif norm_tool_name == "grep":
                    result_message = await asyncio.to_thread(_execute_grep, self, **params)
                elif norm_tool_name == "createtool":
                    tool_instance = CreateTool(self)
                    result_message = await asyncio.to_thread(tool_instance.run, **params)
                elif norm_tool_name == "replacetext":
                    result_message = await asyncio.to_thread(
                        _execute_replace_text, self, **params
                    )
                elif norm_tool_name == "replaceall":
                    result_message = await asyncio.to_thread(
                        _execute_replace_all, self, **params
                    )
                elif norm_tool_name == "insertblock":
                    result_message = await asyncio.to_thread(
                        _execute_insert_block, self, **params
                    )
                elif norm_tool_name == "deleteblock":
                    result_message = await asyncio.to_thread(
                        _execute_delete_block, self, **params
                    )
                elif norm_tool_name == "replaceline":
                    result_message = await asyncio.to_thread(
                        _execute_replace_line, self, **params
                    )
                elif norm_tool_name == "replacelines":
                    result_message = await asyncio.to_thread(
                        _execute_replace_lines, self, **params
                    )
                elif norm_tool_name == "indentlines":
                    result_message = await asyncio.to_thread(
                        _execute_indent_lines, self, **params
                    )
                elif norm_tool_name == "deleteline":
                    result_message = await asyncio.to_thread(
                        _execute_delete_line, self, **params
                    )
                elif norm_tool_name == "deletelines":
                    result_message = await asyncio.to_thread(
                        _execute_delete_lines, self, **params
                    )
                elif norm_tool_name == "undochange":
                    result_message = await asyncio.to_thread(_execute_undo_change, self, **params)
                elif norm_tool_name == "listchanges":
                    result_message = await asyncio.to_thread(
                        _execute_list_changes, self, **params
                    )
                elif norm_tool_name == "extractlines":
                    result_message = await asyncio.to_thread(
                        _execute_extract_lines, self, **params
                    )
                elif norm_tool_name == "shownumberedcontext":
                    result_message = await asyncio.to_thread(
                        execute_show_numbered_context, self, **params
                    )
                elif norm_tool_name == "updatetodolist":
                    result_message = await asyncio.to_thread(
                        _execute_update_todo_list, self, **params
                    )
                elif (
                    hasattr(self, "local_tool_instances")
                    and tool_name in self.local_tool_instances
                ):
                    tool_instance = self.local_tool_instances[tool_name]
                    try:
                        result_message = await tool_instance.run(**params)
                    except Exception as e:
                        result_message = f"Error executing custom tool {tool_name}: {e}"
                        self.io.tool_error(
                            "Error during custom tool"
                            f" {tool_name} execution: {e}\n{traceback.format_exc()}"
                        )
                else:
                    result_message = f"Error: Unknown tool name '{tool_name}'"
                    if self.mcp_tools:
                        for server_name, server_tools in self.mcp_tools:
                            if any(
                                t.get("function", {}).get("name") == tool_name
                                for t in server_tools
                            ):
                                server = next(
                                    (s for s in self.mcp_servers if s.name == server_name), None
                                )
                                if server:
                                    result_message = await self._execute_mcp_tool(
                                        server, tool_name, params
                                    )
                                else:
                                    result_message = (
                                        f"Error: Could not find server instance for {server_name}"
                                    )
                                break

            except Exception as e:
                result_message = f"Error executing {tool_name}: {str(e)}"
                self.io.tool_error(
                    f"Error during {tool_name} execution: {e}\n{traceback.format_exc()}"
                )

            if result_message:
                result_messages.append(f"[Result ({tool_name}): {result_message}]")

            # Note: We don't add the tool call string back to processed_content

        # Update internal counter
        self.tool_call_count += call_count

        # Return the content with tool calls removed
        modified_content = processed_content

        return (
            modified_content,
            result_messages,
            tool_calls_found,
            content_before_separator,
            tool_names,
        )

    def _get_repetitive_tools(self):
        """
        Identifies repetitive tool usage patterns from a flat list of tool calls.

        This method checks for the following patterns in order:
        1. If the last tool used was a write tool, it assumes progress and returns no repetitive tools.
        2. It checks for any read tool that has been used 2 or more times in the history.
        3. If no tools are repeated, but all tools in the history are read tools,
           it flags all of them as potentially repetitive.

        It avoids flagging repetition if a "write" tool was used recently,
        as that suggests progress is being made.
        """
        history_len = len(self.tool_usage_history)

        # Not enough history to detect a pattern
        if history_len < 2:
            return set()

        # If the last tool was a write tool, we're likely making progress.
        if isinstance(self.tool_usage_history[-1], str):
            last_tool_lower = self.tool_usage_history[-1].lower()

            if last_tool_lower in self.write_tools:
                self.tool_usage_history = []
                return set()

        # If all tools in history are read tools, return all of them
        if all(tool.lower() in self.read_tools for tool in self.tool_usage_history):
            return set(tool for tool in self.tool_usage_history)

        # Check for any read tool used more than once
        tool_counts = Counter(tool for tool in self.tool_usage_history)
        repetitive_tools = {
            tool
            for tool, count in tool_counts.items()
            if count >= 2 and tool.lower() in self.read_tools
        }

        if repetitive_tools:
            return repetitive_tools

        return set()

    def _generate_tool_context(self, repetitive_tools):
        """
        Generate a context message for the LLM about recent tool usage.
        """
        if not self.tool_usage_history:
            return ""

        context_parts = ['<context name="tool_usage_history">']

        # Add turn and tool call statistics
        context_parts.append("## Turn and Tool Call Statistics")
        context_parts.append(f"- Current turn: {self.num_reflections + 1}")
        context_parts.append(f"- Tool calls this turn: {self.tool_call_count}")
        context_parts.append(f"- Total tool calls in session: {self.num_tool_calls}")
        context_parts.append("\n\n")

        # Add recent tool usage history
        context_parts.append("## Recent Tool Usage History")
        if len(self.tool_usage_history) > 10:
            recent_history = self.tool_usage_history[-10:]
            context_parts.append("(Showing last 10 tools)")
        else:
            recent_history = self.tool_usage_history

        for i, tool in enumerate(recent_history, 1):
            context_parts.append(f"{i}. {tool}")
        context_parts.append("\n\n")

        if repetitive_tools:
            context_parts.append(
                "**Instruction:**\nYou have used the following tool(s) repeatedly:"
            )

            context_parts.append("### DO NOT USE THE FOLLOWING TOOLS/FUNCTIONS")

            for tool in repetitive_tools:
                context_parts.append(f"- `{tool}`")
            context_parts.append(
                "Your exploration appears to be stuck in a loop. Please try a different approach:"
            )
            context_parts.append("\n")
            context_parts.append("**Suggestions for alternative approaches:**")
            context_parts.append(
                "- If you've been searching for files, try working with the files already in"
                " context"
            )
            context_parts.append(
                "- If you've been viewing files, try making actual edits to move forward"
            )
            context_parts.append("- Consider using different tools that you haven't used recently")
            context_parts.append(
                "- Focus on making concrete progress rather than gathering more information"
            )
            context_parts.append(
                "- Use the files you've already discovered to implement the requested changes"
            )
            context_parts.append("\n")
            context_parts.append(
                "You most likely have enough context for a subset of the necessary changes."
            )
            context_parts.append("Please prioritize file editing over further exploration.")

        context_parts.append("</context>")
        return "\n".join(context_parts)

    async def _apply_edits_from_response(self):
        """
        Parses and applies SEARCH/REPLACE edits found in self.partial_response_content.
        Returns a set of relative file paths that were successfully edited.
        """
        edited_files = set()
        try:
            # 1. Get edits (logic from EditBlockCoder.get_edits)
            # Use the current partial_response_content which contains the LLM response
            # including the edit blocks but excluding the tool calls.
            edits = list(
                find_original_update_blocks(
                    self.partial_response_content,
                    self.fence,
                    self.get_inchat_relative_files(),
                )
            )
            # Separate shell commands from file edits
            self.shell_commands += [edit[1] for edit in edits if edit[0] is None]
            edits = [edit for edit in edits if edit[0] is not None]

            # 2. Prepare edits (check permissions, commit dirty files)
            prepared_edits = []
            seen_paths = dict()
            self.need_commit_before_edits = set()  # Reset before checking

            for edit in edits:
                path = edit[0]
                if path in seen_paths:
                    allowed = seen_paths[path]
                else:
                    # Use the base Coder's permission check method
                    allowed = await self.allowed_to_edit(path)
                    seen_paths[path] = allowed
                if allowed:
                    prepared_edits.append(edit)

            # Commit any dirty files identified by allowed_to_edit
            await self.dirty_commit()
            self.need_commit_before_edits = set()  # Clear after commit

            # 3. Apply edits (logic adapted from EditBlockCoder.apply_edits)
            failed = []
            passed = []
            for edit in prepared_edits:
                path, original, updated = edit
                full_path = self.abs_root_path(path)
                new_content = None

                if Path(full_path).exists():
                    content = self.io.read_text(full_path)
                    # Use the imported do_replace function
                    new_content = do_replace(full_path, content, original, updated, self.fence)

                # Simplified cross-file patching check from EditBlockCoder
                if not new_content and original.strip():
                    for other_full_path in self.abs_fnames:
                        if other_full_path == full_path:
                            continue
                        other_content = self.io.read_text(other_full_path)
                        other_new_content = do_replace(
                            other_full_path, other_content, original, updated, self.fence
                        )
                        if other_new_content:
                            path = self.get_rel_fname(other_full_path)
                            full_path = other_full_path
                            new_content = other_new_content
                            self.io.tool_warning(f"Applied edit intended for {edit[0]} to {path}")
                            break

                if new_content:
                    if not self.dry_run:
                        self.io.write_text(full_path, new_content)
                        self.io.tool_output(f"Applied edit to {path}")
                    else:
                        self.io.tool_output(f"Did not apply edit to {path} (--dry-run)")
                    passed.append((path, original, updated))  # Store path relative to root
                else:
                    failed.append(edit)

            if failed:
                # Handle failed edits (adapted from EditBlockCoder)
                blocks = "block" if len(failed) == 1 else "blocks"
                error_message = f"# {len(failed)} SEARCH/REPLACE {blocks} failed to match!\n"
                for edit in failed:
                    path, original, updated = edit
                    full_path = self.abs_root_path(path)
                    content = self.io.read_text(full_path)  # Read content again for context

                    error_message += f"""
## SearchReplaceNoExactMatch: This SEARCH block failed to exactly match lines in {path}
<<<<<<< SEARCH
{original}=======
{updated}>>>>>>> REPLACE

"""
                    did_you_mean = find_similar_lines(original, content)
                    if did_you_mean:
                        error_message += f"""Did you mean to match some of these actual lines from {path}?

{self.fence[0]}
{did_you_mean}
{self.fence[1]}

"""
                    if updated in content and updated:
                        error_message += f"""Are you sure you need this SEARCH/REPLACE block?
The REPLACE lines are already in {path}!

"""
                error_message += (
                    "The SEARCH section must exactly match an existing block of lines including all"
                    " white space, comments, indentation, docstrings, etc\n"
                )
                if passed:
                    pblocks = "block" if len(passed) == 1 else "blocks"
                    error_message += f"""
# The other {len(passed)} SEARCH/REPLACE {pblocks} were applied successfully.
Don't re-send them.
Just reply with fixed versions of the {blocks} above that failed to match.
"""
                self.io.tool_error(error_message)
                # Set reflected_message to prompt LLM to fix the failed blocks
                self.reflected_message = error_message

            edited_files = set(edit[0] for edit in passed)  # Use relative paths stored in passed

            # 4. Post-edit actions (commit, lint, test, shell commands)
            if edited_files:
                self.aider_edited_files.update(edited_files)  # Track edited files
                await self.auto_commit(edited_files)
                # We don't use saved_message here as we are not moving history back

                if self.auto_lint:
                    lint_errors = self.lint_edited(edited_files)
                    await self.auto_commit(edited_files, context="Ran the linter")
                    if (
                        lint_errors and not self.reflected_message
                    ):  # Reflect only if edits failed
                        ok = await self.io.confirm_ask("Attempt to fix lint errors?")
                        if ok:
                            self.reflected_message = lint_errors

                shared_output = await self.run_shell_commands()
                if shared_output:
                    # Add shell output as a new user message? Or just display?
                    # Let's just display for now to avoid complex history manipulation
                    self.io.tool_output("Shell command output:\n" + shared_output)

                if (
                    self.auto_test and not self.reflected_message
                ):  # Reflect only if no prior errors
                    test_errors = await self.commands.cmd_test(self.test_cmd)
                    if test_errors:
                        ok = await self.io.confirm_ask("Attempt to fix test errors?")
                        if ok:
                            self.reflected_message = test_errors

            self.show_undo_hint()

        except ValueError as err:
            # Handle parsing errors from find_original_update_blocks
            self.num_malformed_responses += 1
            error_message = err.args[0]
            self.io.tool_error("The LLM did not conform to the edit format.")
            self.io.tool_output(urls.edit_errors)
            self.io.tool_output()
            self.io.tool_output(str(error_message))
            self.reflected_message = str(error_message)  # Reflect parsing errors
        except ANY_GIT_ERROR as err:
            self.io.tool_error(f"Git error during edit application: {str(err)}")
            self.reflected_message = f"Git error during edit application: {str(err)}"
        except Exception as err:
            self.io.tool_error("Exception while applying edits:")
            self.io.tool_error(str(err), strip=False)
            traceback.print_exc()
            self.reflected_message = f"Exception while applying edits: {str(err)}"

        return edited_files

    def _add_file_to_context(self, file_path, explicit=False):
        """
        Helper method to add a file to context as read-only.

        Parameters:
        - file_path: Path to the file to add
        - explicit: Whether this was an explicit view command (vs. implicit through ViewFilesAtGlob/ViewFilesMatching)
        """
        # Check if file exists
        abs_path = self.abs_root_path(file_path)
        rel_path = self.get_rel_fname(abs_path)

        if not os.path.isfile(abs_path):
            self.io.tool_output(f"⚠️ File '{file_path}' not found")
            return "File not found"

        # Check if the file is already in context (either editable or read-only)
        if abs_path in self.abs_fnames:
            if explicit:
                self.io.tool_output(f"📎 File '{file_path}' already in context as editable")
                return "File already in context as editable"
            return "File already in context as editable"

        if abs_path in self.abs_read_only_fnames:
            if explicit:
                self.io.tool_output(f"📎 File '{file_path}' already in context as read-only")
                return "File already in context as read-only"
            return "File already in context as read-only"

        # Add file to context as read-only
        try:
            # Check for large file and apply context management if enabled
            content = self.io.read_text(abs_path)
            if content is None:
                return f"Error reading file: {file_path}"

            # Check if file is very large and context management is enabled
            if self.context_management_enabled:
                file_tokens = self.main_model.token_count(content)
                if file_tokens > self.large_file_token_threshold:
                    self.io.tool_output(
                        f"'{file_path}' is very large ({file_tokens} tokens). "
                        "Use /context-management to toggle truncation off if needed."
                    )

            # Add to read-only files
            self.abs_read_only_fnames.add(abs_path)

            # Track in exploration set
            self.files_added_in_exploration.add(rel_path)

            # Inform user
            if explicit:
                self.io.tool_output(f"Viewed '{file_path}' (added to context as read-only)")
                return "Viewed file (added to context as read-only)"
            else:
                # For implicit adds (from ViewFilesAtGlob/ViewFilesMatching), just return success
                return "Added file to context as read-only"

        except Exception as e:
            self.io.tool_error(f"Error adding file '{file_path}' for viewing: {str(e)}")
            return f"Error adding file for viewing: {str(e)}"

    def _process_file_mentions(self, content):
        """
        Process implicit file mentions in the content, adding files if they're not already in context.

        This handles the case where the LLM mentions file paths without using explicit tool commands.
        """
        # Extract file mentions using the parent class's method
        mentioned_files = set(self.get_file_mentions(content, ignore_current=False))
        current_files = set(self.get_inchat_relative_files())

        # Get new files to add (not already in context)
        mentioned_files - current_files

        # In navigator mode, we *only* add files via explicit tool commands (`View`, `ViewFilesAtGlob`, etc.).
        # Do nothing here for implicit mentions.
        pass

    async def check_for_file_mentions(self, content):
        """
        Override parent's method to use our own file processing logic.

        Override parent's method to disable implicit file mention handling in navigator mode.
        Files should only be added via explicit tool commands
        (`View`, `ViewFilesAtGlob`, `ViewFilesMatching`, `ViewFilesWithSymbol`).
        """
        # Do nothing - disable implicit file adds in navigator mode.
        pass

    async def preproc_user_input(self, inp):
        """
        Override parent's method to wrap user input in a context block.
        This clearly delineates user input from other sections in the context window.
        """
        # First apply the parent's preprocessing
        inp = await super().preproc_user_input(inp)

        # If we still have input after preprocessing, wrap it in a context block
        if inp and not inp.startswith('<context name="user_input">'):
            inp = f'<context name="user_input">\n{inp}\n</context>'

        return inp

    def get_directory_structure(self):
        """
        Generate a structured directory listing of the project file structure.
        Returns a formatted string representation of the directory tree.
        """
        if not self.use_enhanced_context:
            return None

        try:
            # Start with the header
            result = '<context name="directoryStructure">\n'
            result += "## Project File Structure\n\n"
            result += (
                "Below is a snapshot of this project's file structure at the current time. It skips"
                " over .gitignore patterns.\n\n"
            )

            # Get the root directory
            Path(self.root)

            # Get all files in the repo (both tracked and untracked)
            if self.repo:
                # Get tracked files
                tracked_files = self.repo.get_tracked_files()

                # Get untracked files (files present in the working directory but not in git)
                untracked_files = []
                try:
                    # Run git status to get untracked files
                    untracked_output = self.repo.repo.git.status("--porcelain")
                    for line in untracked_output.splitlines():
                        if line.startswith("??"):
                            # Extract the filename (remove the '?? ' prefix)
                            untracked_file = line[3:]
                            if not self.repo.git_ignored_file(untracked_file):
                                untracked_files.append(untracked_file)
                except Exception as e:
                    self.io.tool_warning(f"Error getting untracked files: {str(e)}")

                # Combine tracked and untracked files
                all_files = tracked_files + untracked_files
            else:
                # If no repo, get all files relative to root
                all_files = []
                for path in Path(self.root).rglob("*"):
                    if path.is_file():
                        all_files.append(str(path.relative_to(self.root)))

            # Sort files to ensure deterministic output
            all_files = sorted(all_files)

            # Filter out .aider files/dirs
            all_files = [
                f for f in all_files if not any(part.startswith(".aider") for part in f.split("/"))
            ]

            # Build tree structure
            tree = {}
            for file in all_files:
                parts = file.split("/")
                current = tree
                for i, part in enumerate(parts):
                    if i == len(parts) - 1:  # Last part (file)
                        if "." not in current:
                            current["."] = []
                        current["."].append(part)
                    else:  # Directory
                        if part not in current:
                            current[part] = {}
                        current = current[part]

            # Function to recursively print the tree
            def print_tree(node, prefix="- ", indent="  ", current_path=""):
                lines = []
                # First print all directories
                dirs = sorted([k for k in node.keys() if k != "."])
                for i, dir_name in enumerate(dirs):
                    # Only print the current directory name, not the full path
                    lines.append(f"{prefix}{dir_name}/")
                    sub_lines = print_tree(
                        node[dir_name], prefix=prefix, indent=indent, current_path=dir_name
                    )
                    for sub_line in sub_lines:
                        lines.append(f"{indent}{sub_line}")

                # Then print all files
                if "." in node:
                    for file_name in sorted(node["."]):
                        # Only print the current file name, not the full path
                        lines.append(f"{prefix}{file_name}")

                return lines

            # Generate the tree starting from root
            tree_lines = print_tree(tree, prefix="- ")
            result += "\n".join(tree_lines)
            result += "\n</context>"

            return result
        except Exception as e:
            self.io.tool_error(f"Error generating directory structure: {str(e)}")
            return None

    def get_todo_list(self):
        """
        Generate a todo list context block from the .aider.todo.txt file.
        Returns formatted string with the current todo list or None if empty/not present.
        """

        try:
            # Define the todo file path
            todo_file_path = ".aider.todo.txt"
            abs_path = self.abs_root_path(todo_file_path)

            # Check if file exists
            import os

            if not os.path.isfile(abs_path):
                return (
                    '<context name="todo_list">\n'
                    "Todo list does not exist. Please update it."
                    "</context>"
                )

            # Read todo list content
            content = self.io.read_text(abs_path)
            if content is None or not content.strip():
                return None

            # Format the todo list context block
            result = '<context name="todo_list">\n'
            result += "## Current Todo List\n\n"
            result += "Below is the current todo list managed via `UpdateTodoList` tool:\n\n"
            result += f"```\n{content}\n```\n"
            result += "</context>"

            return result
        except Exception as e:
            self.io.tool_error(f"Error generating todo list context: {str(e)}")
            return None

    def get_git_status(self):
        """
        Generate a git status context block for repository information.
        Returns a formatted string with git branch, status, and recent commits.
        """
        if not self.use_enhanced_context or not self.repo:
            return None

        try:
            result = '<context name="gitStatus">\n'
            result += "## Git Repository Status\n\n"
            result += "This is a snapshot of the git status at the current time.\n"

            # Get current branch
            try:
                current_branch = self.repo.repo.active_branch.name
                result += f"Current branch: {current_branch}\n\n"
            except Exception:
                result += "Current branch: (detached HEAD state)\n\n"

            # Get main/master branch
            main_branch = None
            try:
                for branch in self.repo.repo.branches:
                    if branch.name in ("main", "master"):
                        main_branch = branch.name
                        break
                if main_branch:
                    result += f"Main branch (you will usually use this for PRs): {main_branch}\n\n"
            except Exception:
                pass

            # Git status
            result += "Status:\n"
            try:
                # Get modified files
                status = self.repo.repo.git.status("--porcelain")

                # Process and categorize the status output
                if status:
                    status_lines = status.strip().split("\n")

                    # Group by status type for better organization
                    staged_added = []
                    staged_modified = []
                    staged_deleted = []
                    unstaged_modified = []
                    unstaged_deleted = []
                    untracked = []

                    for line in status_lines:
                        if len(line) < 4:  # Ensure the line has enough characters
                            continue

                        status_code = line[:2]
                        file_path = line[3:]

                        # Skip .aider files/dirs
                        if any(part.startswith(".aider") for part in file_path.split("/")):
                            continue

                        # Staged changes
                        if status_code[0] == "A":
                            staged_added.append(file_path)
                        elif status_code[0] == "M":
                            staged_modified.append(file_path)
                        elif status_code[0] == "D":
                            staged_deleted.append(file_path)
                        # Unstaged changes
                        if status_code[1] == "M":
                            unstaged_modified.append(file_path)
                        elif status_code[1] == "D":
                            unstaged_deleted.append(file_path)
                        # Untracked files
                        if status_code == "??":
                            untracked.append(file_path)

                    # Output in a nicely formatted manner
                    if staged_added:
                        for file in staged_added:
                            result += f"A  {file}\n"
                    if staged_modified:
                        for file in staged_modified:
                            result += f"M  {file}\n"
                    if staged_deleted:
                        for file in staged_deleted:
                            result += f"D  {file}\n"
                    if unstaged_modified:
                        for file in unstaged_modified:
                            result += f" M {file}\n"
                    if unstaged_deleted:
                        for file in unstaged_deleted:
                            result += f" D {file}\n"
                    if untracked:
                        for file in untracked:
                            result += f"?? {file}\n"
                else:
                    result += "Working tree clean\n"
            except Exception as e:
                result += f"Unable to get modified files: {str(e)}\n"

            # Recent commits
            result += "\nRecent commits:\n"
            try:
                commits = list(self.repo.repo.iter_commits(max_count=5))
                for commit in commits:
                    short_hash = commit.hexsha[:8]
                    message = commit.message.strip().split("\n")[0]  # First line only
                    result += f"{short_hash} {message}\n"
            except Exception:
                result += "Unable to get recent commits\n"

            result += "</context>"
            return result
        except Exception as e:
            self.io.tool_error(f"Error generating git status: {str(e)}")
            return None

    def cmd_context_blocks(self, args=""):
        """
        Toggle enhanced context blocks feature.
        """
        self.use_enhanced_context = not self.use_enhanced_context

        if self.use_enhanced_context:
            self.io.tool_output(
                "Enhanced context blocks are now ON - directory structure and git status will be"
                " included."
            )
            # Mark tokens as needing calculation, but don't calculate yet (lazy calculation)
            self.tokens_calculated = False
            self.context_blocks_cache = {}
        else:
            self.io.tool_output(
                "Enhanced context blocks are now OFF - directory structure and git status will not"
                " be included."
            )
            # Clear token counts and cache when disabled
            self.context_block_tokens = {}
            self.context_blocks_cache = {}
            self.tokens_calculated = False

        return True

    def cmd_tools_repair(self, args=""):  # noqa: F811
        """
        Repair a custom tool that is erroring.

        /tools-repair <tool_name_or_path> [description of problem]
        """
        if not args:
            self.io.tool_error("Usage: /tools-repair <tool_name_or_path> [description of problem]")
            return

        tool_identifier = args.split(" ", 1)[0]

        tool_file_path = None

        # Check if identifier is a path
        abs_path_identifier = self.abs_root_path(tool_identifier)
        if os.path.isfile(abs_path_identifier):
            tool_file_path = tool_identifier
        # Check if identifier is a tool name
        elif tool_identifier in self.custom_tools:
            tool_file_path = self.custom_tools[tool_identifier].get("file_path")

        if not tool_file_path:
            self.io.tool_error(f"Tool '{tool_identifier}' not found.")
            return

        abs_tool_path = self.abs_root_path(tool_file_path)
        if not os.path.exists(abs_tool_path):
            self.io.tool_error(f"Tool file '{tool_file_path}' not found for tool '{tool_identifier}'.")
            return

        # Add the tool file to the chat
        self.add_rel_fname(tool_file_path)
        self.io.tool_output(f"Added '{tool_file_path}' to the chat to be repaired.")

        # Mark for reload after fix
        self.tool_file_to_reload_after_fix = tool_file_path

        return True

    def cmd_copy_context(self, args=None):
        """Copy the current chat context as markdown, suitable to paste into a web UI"""

        chunks = self.format_chat_chunks()

        markdown = ""

        # Only include specified chunks in order
        for messages in [chunks.repo, chunks.readonly_files, chunks.chat_files]:
            for msg in messages:
                # Only include user messages
                if msg["role"] != "user":
                    continue

                content = msg["content"]

                # Handle image/multipart content
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            markdown += part["text"] + "\n\n"
                else:
                    markdown += content + "\n\n"

        args = args or ""
        markdown += f"""
Just tell me how to edit the files to make the changes.
Don't give me back entire files.
Just show me the edits I need to make.

{args}
"""

        try:
            if pyperclip:
                pyperclip.copy(markdown)
                self.io.tool_output("Copied code context to clipboard.")
            else:
                self.io.tool_error("pyperclip is not installed.")
                self.io.tool_output(
                    "You may need to install xclip or xsel on Linux, or pbcopy on macOS."
                )

        except pyperclip.PyperclipException as e:
            self.io.tool_error(f"Failed to copy to clipboard: {str(e)}")
            self.io.tool_output(
                "You may need to install xclip or xsel on Linux, or pbcopy on macOS."
            )
        except Exception as e:
            self.io.tool_error(f"An unexpected error occurred while copying to clipboard: {str(e)}")


def expand_subdir(file_path):
    if file_path.is_file():
        yield file_path
        return

    if file_path.is_dir():
        for file in file_path.rglob("*"):
            if file.is_file():
                yield file


def parse_quoted_filenames(args):
    filenames = re.findall(r'"(.+?)"|(\S+)', args)
    filenames = [name for sublist in filenames for name in sublist if name]
    return filenames
