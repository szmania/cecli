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
from aiderer.mcp.server import LocalServer
from aider.repo import ANY_GIT_ERROR
from aider.tools.base_tool import BaseAiderTool
from aider.tools.create_tool import CreateTool
from aider.tools import (
    command,
    command_interactive,
    delete_block,
    delete_line,
    delete_lines,
    extract_lines,
    finished,
    git_diff,
    git_log,
    git_show,
    git_status,
    grep,
    indent_lines,
    insert_block,
    list_changes,
    ls,
    make_editable,
    make_readonly,
    remove,
    replace_all,
    replace_line,
    replace_lines,
    replace_text,
    show_numbered_context,
    undo_change,
    update_todo_list,
    view,
    view_files_matching,
    view_files_with_symbol,
)

from .agent_prompts import AgentPrompts
from .base_coder import ChatChunks, Coder
from .editblock_coder import do_replace, find_original_update_blocks, find_similar_lines

try:
    import pyperclip
except ImportError:
    pyperclip = None


class AgentCoder(Coder):
    """Mode where the LLM autonomously manages which files are in context."""

    edit_format = "agent"

    def __init__(self, *args, **kwargs):
        # Initialize appropriate prompt set before calling parent constructor
        # This needs to happen before super().__init__ so the parent class has access to gpt_prompts
        self.gpt_prompts = AgentPrompts()

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
        # Will be overridden by agent_config if provided
        self.large_file_token_threshold = (
            25000  # Files larger than this in tokens are considered large
        )

        # Enable context management by default only in agent mode
        self.context_management_enabled = True  # Enabled by default for agent mode

        # Initialize change tracker for granular editing
        self.change_tracker = ChangeTracker()

        # Initialize tool registry
        self.args = kwargs.get("args")
        self._tool_registry = self._build_tool_registry()

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

        self.skip_cli_confirmations = False

        self._get_agent_config()
        super().__init__(*args, **kwargs)

    def _build_tool_registry(self):
        """
        Build a registry of available tools with their normalized names and process_response functions.
        Handles agent configuration with includelist/excludelist functionality.

        Returns:
            dict: Mapping of normalized tool names to tool modules
        """
        registry = {}

        # Add tools that have been imported
        tool_modules = [
            command,
            command_interactive,
            delete_block,
            delete_line,
            delete_lines,
            extract_lines,
            finished,
            git_diff,
            git_log,
            git_show,
            git_status,
            grep,
            indent_lines,
            insert_block,
            list_changes,
            ls,
            make_editable,
            make_readonly,
            remove,
            replace_all,
            replace_line,
            replace_lines,
            replace_text,
            show_numbered_context,
            undo_change,
            update_todo_list,
            view,
            view_files_matching,
            view_files_with_symbol,
        ]

        # Process agent configuration if provided
        agent_config = self._get_agent_config()
        tools_includelist = agent_config.get(
            "tools_includelist", agent_config.get("tools_whitelist", [])
        )
        tools_excludelist = agent_config.get(
            "tools_excludelist", agent_config.get("tools_blacklist", [])
        )

        # Always include essential tools regardless of includelist/excludelist
        essential_tools = {"makeeditable", "replacetext", "view", "finished"}
        for module in tool_modules:
            if hasattr(module, "NORM_NAME") and hasattr(module, "process_response"):
                tool_name = module.NORM_NAME

                # Check if tool should be included based on configuration
                should_include = True

                # If includelist is specified, only include tools in includelist
                if tools_includelist:
                    should_include = tool_name in tools_includelist

                # Always include essential tools
                if tool_name in essential_tools:
                    should_include = True

                # Exclude tools in excludelist (unless they're essential)
                if tool_name in tools_excludelist and tool_name not in essential_tools:
                    should_include = False

                if should_include:
                    registry[tool_name] = module

        return registry

    def _get_agent_config(self):
        """
        Parse and return agent configuration from args.agent_config.

        Returns:
            dict: Agent configuration with defaults for missing values
        """
        config = {}

        # Check if agent_config is provided via args
        if (
            hasattr(self, "args")
            and self.args
            and hasattr(self.args, "agent_config")
            and self.args.agent_config
        ):
            try:
                config = json.loads(self.args.agent_config)
            except (json.JSONDecodeError, TypeError) as e:
                self.io.tool_warning(f"Failed to parse agent-config JSON: {e}")
                return {}

        # Set defaults for missing values
        if "large_file_token_threshold" not in config:
            config["large_file_token_threshold"] = 25000
        if "tools_includelist" not in config:
            config["tools_includelist"] = []
        if "tools_excludelist" not in config:
            config["tools_excludelist"] = []

        # Apply configuration to instance
        self.large_file_token_threshold = config["large_file_token_threshold"]
        self.skip_cli_confirmations = config.get(
            "skip_cli_confirmations", config.get("yolo", False)
        )

        return config

    def get_local_tool_schemas(self):
        """Returns the JSON schemas for all local tools using the tool registry."""
        schemas = []

        # Get schemas from the tool registry
        for tool_module in self._tool_registry.values():
            if hasattr(tool_module, "schema"):
                schemas.append(tool_module.schema)

        # Add git schemas from the tool registry
        git_tools = [git_diff, git_log, git_show, git_status]
        for git_tool in git_tools:
            if hasattr(git_tool, "schema"):
                schemas.append(git_tool.schema)

        # Add CreateTool schema manually as it's a class-based tool
        schemas.append(CreateTool(self).schema)

        return schemas

    async def initialize_mcp_tools(self):
        await super().initialize_mcp_tools()

        local_tools = self.get_local_tool_schemas()
        if not local_tools:
            return

        local_server_config = {"name": "local_tools"}
        local_server = LocalServer(local_server_config)

        if not self.mcp_servers:
            self.mcp_servers = []
        if not any(isinstance(s, LocalServer) for s in self.mcp_servers):
            self.mcp_servers.append(local_server)

        if not self.mcp_tools:
            self.mcp_tools = []

        # Update or add the "local_tools" entry
        local_tools_entry_index = -1
        for i, (server_name, _) in enumerate(self.mcp_tools):
            if server_name == "local_tools":
                local_tools_entry_index = i
                break

        if local_tools_entry_index != -1:
            self.mcp_tools[local_tools_entry_index] = (local_server.name, local_tools)
        else:
            self.mcp_tools.append((local_server.name, local_tools))

        self.functions = self.get_tool_list()

    def validate_tool_definition(self, tool_definition):
        """
        Validates a tool definition against a schema and checks its parameters.
        """
        tool_schema = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["function"]},
                "function": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "pattern": r"^[a-zA-Z0-9_]{1,64}$"},
                        "description": {"type": "string"},
                        "parameters": {"type": "object"},
                    },
                    "required": ["name", "description", "parameters"],
                    "additionalProperties": False,
                },
            },
            "required": ["type", "function"],
            "additionalProperties": False,
        }

        try:
            jsonschema.validate(instance=tool_definition, schema=tool_schema)
            parameters_schema = tool_definition["function"]["parameters"]
            jsonschema.Draft7Validator.check_schema(parameters_schema)
        except jsonschema.ValidationError as e:
            raise ValueError(f"Invalid tool definition: {e.message} at {e.path}")
        except jsonschema.SchemaError as e:
            raise ValueError(f"Invalid JSON schema in tool parameters: {e.message} at {e.path}")
        except Exception as e:
            raise ValueError(f"Unexpected error during tool definition validation: {e}")

    def _get_global_tools_dir(self):
        return (Path.home() / ".aider.tools").resolve()

    def _get_local_tools_dir(self):
        if self.repo and self.repo.root:
            return (Path(self.repo.root) / ".aider.tools").resolve()
        else:
            return (Path.cwd() / ".aider.tools").resolve()

    def tool_add_from_path(self, file_path: str):
        if not file_path.lower().endswith(".py"):
            self.io.tool_error(f"Tool file must be a Python file: {file_path}")
            return

        abs_file_path = self.abs_root_path(file_path)
        if not os.path.exists(abs_file_path):
            self.io.tool_error(f"Tool file not found: {file_path}")
            return

        try:
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

            tool_instance = tool_class(self)
            tool_name = tool_instance.name
            tool_definition = tool_instance.schema
            self.validate_tool_definition(tool_definition)

            self.local_tool_instances[tool_name] = tool_instance

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
                updated_tools.append(tool_definition)
                self.mcp_tools[local_tools_entry_index] = ("local_tools", updated_tools)

            self.functions = self.get_tool_list()

            self.custom_tools[tool_name] = {
                "path": file_path,
                "description": tool_instance.description,
                "scope": tool_instance.scope,
            }

            self.io.tool_output(
                f"Successfully loaded tool: {tool_name} from {self.get_rel_fname(file_path)}"
            )

        except Exception as e:
            rel_file_path = self.get_rel_fname(file_path)
            self.tool_file_to_reload_after_fix = rel_file_path
            self.io.tool_error(f"Error loading tool from {rel_file_path}: {e}")

            if self.io.confirm_ask(
                f"Do you want to try and fix the error in {rel_file_path} now?"
            ):
                self.add_rel_fname(rel_file_path)
                self.io.tool_output(f"Added {rel_file_path} to the chat to be repaired.")
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
            if tool_name in self.custom_tools:
                del self.custom_tools[tool_name]
            if tool_name in self.local_tool_instances:
                del self.local_tool_instances[tool_name]

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

        self.functions = self.get_tool_list()

    def tool_move(self, file_path: str):
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

        destination_dir = local_tools_dir if is_global else global_tools_dir
        scope_change = "global to local" if is_global else "local to global"
        self.io.tool_output(f"Moving tool '{original_rel_path}' from {scope_change} scope...")

        destination_path = destination_dir / original_abs_path.name
        destination_rel_path = self.get_rel_fname(destination_path)

        if destination_path.exists():
            raise FileExistsError(
                f"A file named '{destination_rel_path}' already exists at the destination."
            )

        self.tool_unload_from_path(str(original_abs_path))

        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(original_abs_path, destination_path)
            self.io.tool_output(f"File moved to: {destination_rel_path}")
        except Exception as e:
            raise IOError(
                f"Failed to move file from {original_rel_path} to {destination_rel_path}: {e}"
            )

        self.tool_add_from_path(str(destination_path))
        self.io.tool_output(f"Reloaded tool '{original_abs_path.name}' with new scope.")

    async def _execute_local_tool_calls(self, tool_calls_list):
        tool_responses = []
        for tool_call in tool_calls_list:
            tool_name = tool_call.function.name
            result_message = ""
            try:
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
                    parsed_args_list.append({})

                all_results_content = []
                tasks = []

                for params in parsed_args_list:
                    norm_tool_name = tool_name.lower()

                    if norm_tool_name == "createtool":
                        tool_instance = CreateTool(self)
                        tasks.append(asyncio.to_thread(tool_instance.run, **params))
                        continue

                    if tool_name in self.local_tool_instances:
                        tool_instance = self.local_tool_instances[tool_name]
                        tasks.append(tool_instance.run(**params))
                        continue

                    if norm_tool_name in self._tool_registry:
                        tool_module = self._tool_registry[norm_tool_name]
                        result = tool_module.process_response(self, params)
                        if asyncio.iscoroutine(result):
                            tasks.append(result)
                        else:
                            tasks.append(asyncio.to_thread(lambda: result))
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
                    if hasattr(item, "resource"):
                        resource = item.resource
                        if hasattr(resource, "text"):
                            content_parts.append(resource.text)
                        elif hasattr(resource, "blob"):
                            try:
                                decoded_blob = base64.b64decode(resource.blob).decode("utf-8")
                                content_parts.append(decoded_blob)
                            except (UnicodeDecodeError, TypeError):
                                name = getattr(resource, "name", "unnamed")
                                mime_type = getattr(resource, "mimeType", "unknown mime type")
                                content_parts.append(
                                    f"[embedded binary resource: {name} ({mime_type})]"
                                )
                    elif hasattr(item, "text"):
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
        if hasattr(self, "tokens_calculated") and self.tokens_calculated and not force:
            return

        self.context_block_tokens = {}
        if not hasattr(self, "context_blocks_cache"):
            self.context_blocks_cache = {}

        if not self.use_enhanced_context:
            return

        try:
            self.context_blocks_cache = {}
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

            self.tokens_calculated = True
        except Exception:
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

        if content is not None:
            self.context_blocks_cache[block_name] = content

        return content

    def get_cached_context_block(self, block_name):
        """
        Get a context block from the cache, or generate it if not available.
        This should be used by format_chat_chunks to avoid regenerating blocks.

        This will ensure tokens are calculated if they haven't been yet.
        """
        if not hasattr(self, "tokens_calculated") or not self.tokens_calculated:
            self._calculate_context_block_tokens()

        if hasattr(self, "context_blocks_cache") and block_name in self.context_blocks_cache:
            return self.context_blocks_cache[block_name]

        return self._generate_context_block(block_name)

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

            if not self.repo_map:
                self.io.tool_warning("RepoMap not initialized, cannot generate symbol outline.")
                return None

            for abs_fname in sorted(files_to_outline):
                rel_fname = self.get_rel_fname(abs_fname)
                try:
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
                        kind_to_check = tag.specific_kind or tag.kind
                        if (
                            kind_to_check
                            and kind_to_check.lower() in self.repo_map.definition_kinds
                        ):
                            definition_tags.append(tag)

                    if definition_tags:
                        result += f"### {rel_fname}\n"
                        for tag in definition_tags:
                            line_info = f", line {tag.line + 1}" if tag.line >= 0 else ""
                            kind_to_check = tag.specific_kind or tag.kind
                            result += f"- {tag.name} ({kind_to_check}{line_info})\n"
                        result += "\n"

            result += "</context>"
            return result.strip()

        except Exception as e:
            self.io.tool_error(f"Error generating symbol outline: {str(e)}")
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
        chunks = super().format_chat_chunks()

        if not self.use_enhanced_context:
            return chunks

        self._calculate_context_block_tokens()

        env_context = self.get_cached_context_block("environment_info")
        dir_structure = self.get_cached_context_block("directory_structure")
        git_status = self.get_cached_context_block("git_status")
        symbol_outline = self.get_cached_context_block("symbol_outline")
        todo_list = self.get_cached_context_block("todo_list")
        context_summary = self.get_context_summary()

        static_blocks = []
        if dir_structure:
            static_blocks.append(dir_structure)
        if env_context:
            static_blocks.append(env_context)

        if static_blocks:
            static_message = "\n\n".join(static_blocks)
            chunks.done.insert(0, dict(role="system", content=static_message))

        dynamic_blocks = []
        if todo_list:
            dynamic_blocks.append(todo_list)
        if context_summary:
            dynamic_blocks.append(context_summary)
        if symbol_outline:
            dynamic_blocks.append(symbol_outline)
        if git_status:
            dynamic_blocks.append(git_status)

        if hasattr(self, "tool_usage_history") and self.tool_usage_history:
            repetitive_tools = self._get_repetitive_tools()
            if repetitive_tools:
                tool_context = self._generate_tool_context(repetitive_tools)
                if tool_context:
                    dynamic_blocks.append(tool_context)

        if dynamic_blocks:
            dynamic_message = "\n\n".join(dynamic_blocks)
            chunks.chat_files.append(dict(role="system", content=dynamic_message))

        return chunks

    def get_context_summary(self):
        """
        Generate a summary of the current context, including file content tokens and additional context blocks,
        with an accurate total token count.
        """
        if not self.use_enhanced_context:
            return None

        if hasattr(self, "context_blocks_cache") and "context_summary" in self.context_blocks_cache:
            return self.context_blocks_cache["context_summary"]

        try:
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
            current_date = datetime.now().strftime("%Y-%m-%d")
            platform_info = platform.platform()
            language = self.chat_language or locale.getlocale()[0] or "en-US"

            result = '<context name="environment_info">\n'
            result += "## Environment Information\n\n"
            result += f"- Working directory: {self.root}\n"
            result += f"- Current date: {current_date}\n"
            result += f"- Platform: {platform_info}\n"
            result += f"- Language preference: {language}\n"

            if self.repo:
                try:
                    rel_repo_dir = self.repo.get_rel_repo_dir()
                    num_files = len(self.repo.get_tracked_files())
                    result += f"- Git repository: {rel_repo_dir} with {num_files:,} files\n"
                except Exception:
                    result += "- Git repository: active but details unavailable\n"
            else:
                result += "- Git repository: none\n"

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
        self.auto_save_session()
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
        self.agent_finished = False
        content = self.partial_response_content
        if not content or not content.strip():
            if len(self.tool_usage_history) > self.tool_usage_retries:
                self.tool_usage_history = []
            return True
        original_content = content

        (
            processed_content,
            result_messages,
            tool_calls_found,
            content_before_last_separator,
            tool_names_this_turn,
        ) = await self._process_tool_commands(content)

        if self.agent_finished:
            self.tool_usage_history = []
            return True

        self.partial_response_content = processed_content.strip()
        self._process_file_mentions(processed_content)

        has_search = "<<<<<<< SEARCH" in self.partial_response_content
        has_divider = "=======" in self.partial_response_content
        has_replace = ">>>>>>> REPLACE" in self.partial_response_content
        edit_match = has_search and has_divider and has_replace

        separator_marker = "\n---\n"
        if separator_marker in original_content and edit_match:
            has_search_before = "<<<<<<< SEARCH" in content_before_last_separator
            has_divider_before = "=======" in content_before_last_separator
            has_replace_before = ">>>>>>> REPLACE" in content_before_last_separator
            edit_match = has_search_before and has_divider_before and has_replace_before

        if edit_match:
            self.io.tool_output("Detected edit blocks, applying changes within Agent...")
            edited_files = await self._apply_edits_from_response()
            if self.reflected_message:
                return False

            if edited_files and self.num_reflections < self.max_reflections:
                original_question = "Please continue your exploration and provide a final answer."
                if self.cur_messages and len(self.cur_messages) >= 1:
                    for msg in reversed(self.cur_messages):
                        if msg["role"] == "user":
                            original_question = msg["content"]
                            break

                next_prompt = (
                    "I have applied the edits you suggested. "
                    f"The following files were modified: {', '.join(edited_files)}. "
                    "Let me continue working on your request.\n\n"
                    f"Your original question was: {original_question}"
                )

                self.reflected_message = next_prompt
                self.io.tool_output("Continuing after applying edits...")
                return False

        if tool_calls_found and self.num_reflections < self.max_reflections:
            self.tool_call_count = 0
            self.files_added_in_exploration = set()

            original_question = "Please continue your exploration and provide a final answer."
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

        if self.files_edited_by_tools:
            saved_message = await self.auto_commit(self.files_edited_by_tools)
            if not saved_message and hasattr(self.gpt_prompts, "files_content_gpt_edits_no_repo"):
                saved_message = self.gpt_prompts.files_content_gpt_edits_no_repo
            self.move_back_cur_messages(saved_message)

        self.tool_call_count = 0
        self.files_added_in_exploration = set()
        self.files_edited_by_tools = set()
        self.move_back_cur_messages(
            None
        )  # Pass None as we handled commit message earlier if needed

        return False  # Always Loop Until the Finished Tool is Called

    def post_edit_actions(self):
        """
        Hook for actions to be performed after edits are applied in a turn.
        This implementation checks if a failed tool file was fixed and prompts for reload.
        """
        if self.tool_file_to_reload_after_fix:
            rel_file_path = self.tool_file_to_reload_after_fix
            if rel_file_path in self.aider_edited_files:
                self.tool_file_to_reload_after_fix = None
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

    async def _execute_tool_with_registry(self, norm_tool_name, params):
        """
        Execute a tool using the tool registry.

        Args:
            norm_tool_name: Normalized tool name (lowercase)
            params: Dictionary of parameters

        Returns:
            str: Result message
        """
        if norm_tool_name in self._tool_registry:
            tool_module = self._tool_registry[norm_tool_name]
            try:
                result = tool_module.process_response(self, params)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            except Exception as e:
                self.io.tool_error(
                    f"Error during {norm_tool_name} execution: {e}\n{traceback.format_exc()}"
                )
                return f"Error executing {norm_tool_name}: {str(e)}"

        if self.mcp_tools:
            for server_name, server_tools in self.mcp_tools:
                if any(t.get("function", {}).get("name") == norm_tool_name for t in server_tools):
                    server = next((s for s in self.mcp_servers if s.name == server_name), None)
                    if server:
                        return await self._execute_mcp_tool(server, norm_tool_name, params)
                    else:
                        return f"Error: Could not find server instance for {server_name}"

        return f"Error: Unknown tool name '{norm_tool_name}'"

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
        modified_content = content
        tool_calls_found = False
        call_count = 0
        max_calls = self.max_tool_calls
        tool_names = []

        separator_marker = "---"
        content_parts = content.split(separator_marker)

        if len(content_parts) == 1:
            return content, result_messages, False, content, tool_names

        content_before_separator = separator_marker.join(content_parts[:-1])
        content_after_separator = content_parts[-1]

        processed_content = content_before_separator + separator_marker
        last_index = 0

        tool_call_pattern = re.compile(r"\[tool_.*?\(", re.DOTALL)
        end_marker = "]"

        while True:
            match = tool_call_pattern.search(content_after_separator, last_index)
            if not match:
                processed_content += content_after_separator[last_index:]
                break

            start_pos = match.start()
            start_marker = match.group(0)

            backslashes = 0
            p = start_pos - 1
            while p >= 0 and content_after_separator[p] == "\\":
                backslashes += 1
                p -= 1

            if backslashes % 2 == 1:
                processed_content += content_after_separator[
                    last_index : start_pos + len(start_marker)
                ]
                last_index = start_pos + len(start_marker)
                continue

            processed_content += content_after_separator[last_index:start_pos]

            scan_start_pos = start_pos + len(start_marker)
            paren_level = 1
            in_single_quotes = False
            in_double_quotes = False
            escaped = False
            end_paren_pos = -1

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

            expected_end_marker_start = end_paren_pos + 1
            actual_end_marker_start = -1
            end_marker_found = False
            if end_paren_pos != -1:
                for j in range(expected_end_marker_start, len(content_after_separator)):
                    if not content_after_separator[j].isspace():
                        actual_end_marker_start = j
                        if content_after_separator[actual_end_marker_start] == end_marker:
                            end_marker_found = True
                        break

            if not end_marker_found:
                tool_name = "unknown"
                try:
                    partial_content = content_after_separator[
                        scan_start_pos : scan_start_pos + 100
                    ]
                    comma_pos = partial_content.find(",")
                    if comma_pos > 0:
                        tool_name = partial_content[:comma_pos].strip()
                    else:
                        space_pos = partial_content.find(" ")
                        paren_pos = partial_content.find("(")
                        if space_pos > 0 and (paren_pos < 0 or space_pos < paren_pos):
                            tool_name = partial_content[:space_pos].strip()
                        elif paren_pos > 0:
                            tool_name = partial_content[:paren_pos].strip()
                except Exception:
                    pass

                self.io.tool_warning(
                    f"Malformed tool call for '{tool_name}'. Missing closing parenthesis or"
                    " bracket. Skipping."
                )
                processed_content += start_marker
                last_index = scan_start_pos
                continue

            full_match_str = content_after_separator[start_pos : actual_end_marker_start + 1]
            inner_content = content_after_separator[scan_start_pos:end_paren_pos].strip()
            last_index = actual_end_marker_start + 1

            call_count += 1
            if call_count > max_calls:
                self.io.tool_warning(
                    f"Exceeded maximum tool calls ({max_calls}). Skipping remaining calls."
                )
                continue

            tool_calls_found = True
            tool_name = None
            params = {}
            result_message = None

            try:
                if inner_content:
                    parts = inner_content.split(",", 1)
                    potential_tool_name = parts[0].strip()
                    is_string = (
                        potential_tool_name.startswith("'") and potential_tool_name.endswith("'")
                    ) or (
                        potential_tool_name.startswith('"') and potential_tool_name.endswith('"')
                    )
                    if not potential_tool_name.isidentifier() and not is_string:
                        quoted_tool_name = json.dumps(potential_tool_name)
                        if len(parts) > 1:
                            inner_content = quoted_tool_name + ", " + parts[1]
                        else:
                            inner_content = quoted_tool_name

                parse_str = f"f({inner_content})"
                parsed_ast = ast.parse(parse_str)

                if (
                    not isinstance(parsed_ast, ast.Module)
                    or not parsed_ast.body
                    or not isinstance(parsed_ast.body[0], ast.Expr)
                ):
                    raise ValueError("Unexpected AST structure")
                call_node = parsed_ast.body[0].value
                if not isinstance(call_node, ast.Call):
                    raise ValueError("Expected a Call node")

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

                for keyword in call_node.keywords:
                    key = keyword.arg
                    value_node = keyword.value
                    if isinstance(value_node, ast.Constant):
                        value = value_node.value
                        if isinstance(value, str) and "\n" in value:
                            lineno = getattr(value_node, "lineno", 0)
                            end_lineno = getattr(value_node, "end_lineno", lineno)
                            if end_lineno > lineno:
                                if value.startswith("\n"):
                                    value = value[1:]
                                if value.endswith("\n"):
                                    value = value[:-1]
                    elif isinstance(value_node, ast.Name):
                        id_val = value_node.id.lower()
                        if id_val == "true":
                            value = True
                        elif id_val == "false":
                            value = False
                        elif id_val == "none":
                            value = None
                        else:
                            value = value_node.id
                    else:
                        try:
                            value = ast.unparse(value_node)
                        except AttributeError:
                            raise ValueError(
                                f"Unsupported argument type for key '{key}': {type(value_node)}"
                            )
                        except Exception as unparse_e:
                            raise ValueError(
                                f"Could not unparse value for key '{key}': {unparse_e}"
                            )

                    if isinstance(value, str):
                        params[key] = value
                    else:
                        params[key] = value

                norm_tool_name = tool_name.lower()
                result_message = await self._execute_tool_with_registry(norm_tool_name, params)

            except Exception as e:
                result_message = f"Error parsing or executing tool call: {e}"
                self.io.tool_error(f"Tool call error: {e}\n{traceback.format_exc()}")

            if result_message:
                result_messages.append(f"[Result ({tool_name})]:\n{result_message}")

        return processed_content, result_messages, tool_calls_found, content_before_separator, tool_names

    def _process_file_mentions(self, content):
        """
        Check for file mentions in the content and add them to the chat if confirmed by the user.
        """
        mentioned_files = self.get_file_mentions(content)
        if mentioned_files:
            for file_path in mentioned_files:
                if self.io.confirm_ask(f"Add {file_path} to the chat?"):
                    self.add_rel_fname(file_path)

    async def _apply_edits_from_response(self):
        """
        Apply edits from SEARCH/REPLACE blocks in the response content.
        """
        content = self.partial_response_content
        if not content:
            return []

        edits = self.get_edits_from_content(content)
        if not edits:
            return []

        edited_files = set()
        for path, original, updated in edits:
            if await self.allowed_to_edit(path):
                file_content = self.io.read_text(self.abs_root_path(path))
                if file_content is None:
                    self.io.tool_error(f"Could not read file {path} to apply edits.")
                    continue

                new_content = file_content.replace(original, updated)
                if new_content != file_content:
                    self.io.write_text(self.abs_root_path(path), new_content)
                    edited_files.add(path)
                    self.io.tool_output(f"Applied edit to {path}")
                else:
                    self.io.tool_warning(f"Edit for {path} did not change the file content.")
            else:
                self.io.tool_warning(f"Skipping edits to {path} as it was not allowed.")

        return list(edited_files)

    def get_edits_from_content(self, content):
        """
        Extract edits from SEARCH/REPLACE blocks in the content.
        """
        edits = []
        file_content_for_edits = self.get_files_content()
        original_update_blocks = find_original_update_blocks(content)

        for original_text, updated_text in original_update_blocks:
            found_in_files = []
            for fname, fcontent in self.get_abs_fnames_content():
                if original_text in fcontent:
                    found_in_files.append(self.get_rel_fname(fname))

            if len(found_in_files) == 1:
                edits.append((found_in_files[0], original_text, updated_text))
            elif len(found_in_files) > 1:
                self.io.tool_warning(
                    f"Found the original text in multiple files: {', '.join(found_in_files)}. Skipping this edit."
                )
            else:
                similar_lines = find_similar_lines(original_text, file_content_for_edits)
                if similar_lines:
                    self.io.tool_warning(
                        "Could not find the original text. Did you mean one of these?\n"
                        + "\n".join(similar_lines)
                    )
                else:
                    self.io.tool_warning("Could not find the original text. Skipping this edit.")
        return edits

    def _get_repetitive_tools(self):
        """
        Identify tools that are being used repetitively.
        """
        if len(self.tool_usage_history) < 4:
            return []

        last_four = self.tool_usage_history[-4:]
        counts = Counter(last_four)
        repetitive = [tool for tool, count in counts.items() if count >= 2]
        return repetitive

    def _generate_tool_context(self, repetitive_tools):
        """
        Generate a context block with information about repetitive tool usage.
        """
        if not repetitive_tools:
            return None

        result = '<context name="tool_usage_feedback">\n'
        result += "## Tool Usage Feedback\n\n"
        result += "You seem to be using the following tools repetitively:\n"
        for tool in repetitive_tools:
            result += f"- {tool}\n"
        result += "\nConsider if there's a more direct way to achieve your goal.\n"
        result += "</context>"
        return result

    def get_directory_structure(self):
        """
        Generate a directory structure context block.
        """
        if not self.use_enhanced_context:
            return None

        try:
            result = '<context name="directory_structure">\n'
            result += "## Directory Structure\n\n"
            result += "```\n"
            for root, dirs, files in os.walk(self.root):
                # Exclude .git and other common metadata directories
                dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", ".vscode"]]
                level = root.replace(self.root, "").count(os.sep)
                indent = " " * 4 * level
                result += f"{indent}{os.path.basename(root)}/\n"
                sub_indent = " " * 4 * (level + 1)
                for f in files:
                    result += f"{sub_indent}{f}\n"
            result += "```\n"
            result += "</context>"
            return result
        except Exception as e:
            self.io.tool_error(f"Error generating directory structure: {str(e)}")
            return None

    def get_git_status(self):
        """
        Generate a git status context block.
        """
        if not self.use_enhanced_context or not self.repo:
            return None

        try:
            status = self.repo.repo.git.status("--porcelain")
            if not status:
                return None

            result = '<context name="git_status">\n'
            result += "## Git Status\n\n"
            result += "```\n"
            result += status
            result += "\n```\n"
            result += "</context>"
            return result
        except ANY_GIT_ERROR as e:
            self.io.tool_warning(f"Could not get git status: {e}")
            return None
        except Exception as e:
            self.io.tool_error(f"Error generating git status: {str(e)}")
            return None

    def get_todo_list(self):
        """
        Generate a todo list context block from the .aider.todo.txt file.
        """
        if not self.use_enhanced_context:
            return None

        todo_file_path = ".aider.todo.txt"
        abs_path = self.abs_root_path(todo_file_path)
        if not os.path.isfile(abs_path):
            return None

        try:
            with open(abs_path, "r") as f:
                content = f.read()
            if not content.strip():
                return None

            result = '<context name="todo_list">\n'
            result += "## TODO List\n\n"
            result += content
            result += "\n</context>"
            return result
        except Exception as e:
            self.io.tool_error(f"Error reading todo list: {str(e)}")
            return None
