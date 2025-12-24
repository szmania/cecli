import difflib
import os
import re
import subprocess
import sys
import importlib.util
from pathlib import Path

from aider.tools.utils.base_tool import BaseTool


class ToolManager:
    def __init__(self, coder):
        self.coder = coder
        self.tools = {}
        self.unloaded_tools = set()
        self.load_dot_env_files()
        self.discovered_tool_files = self.discover_tools()

    def find_tool(self, tool_name_query):
        """
        Find a tool by fuzzy matching the tool name query.
        
        Args:
            tool_name_query (str): The user's tool name query
            
        Returns:
            tuple: (success: bool, tool_name: str, message: str)
                   - If success is True, tool_name contains the matched tool name
                   - If success is False, message contains error information
        """
        if not tool_name_query:
            return False, None, "No tool name provided."
            
        # Get all available tool names (custom tools + standard tools)
        all_tool_names = set()
        
        # Add custom tool names
        for tool_info in self.tools.values():
            all_tool_names.add(tool_info["name"])
            
        # Add standard tool names if coder has tool_registry
        if hasattr(self.coder, 'tool_registry'):
            for tool_class in self.coder.tool_registry.values():
                schema = tool_class.SCHEMA
                name = schema.get("function", {}).get("name")
                if name:
                    all_tool_names.add(name)
                    
        # Add unloaded standard tools if available
        if hasattr(self.coder, 'unloaded_standard_tools'):
            for tool_class in self.coder.unloaded_standard_tools.values():
                schema = tool_class.SCHEMA
                name = schema.get("function", {}).get("name")
                if name:
                    all_tool_names.add(name)
        
        if not all_tool_names:
            return False, None, "No tools available."
            
        # Normalize the query and tool names for comparison
        def normalize_name(name):
            # Split CamelCase and snake_case
            name = re.sub('([a-z0-9])([A-Z])', r'\1 \2', name)
            name = name.replace('_', ' ')
            return name.lower().split()
            
        query_words = normalize_name(tool_name_query)
        
        # Calculate similarity scores
        matches = []
        for tool_name in all_tool_names:
            tool_words = normalize_name(tool_name)
            
            # Calculate word-level similarity
            score = 0
            for q_word in query_words:
                best_match = 0
                for t_word in tool_words:
                    # Use difflib for partial matching
                    similarity = difflib.SequenceMatcher(None, q_word, t_word).ratio()
                    if similarity > best_match:
                        best_match = similarity
                score += best_match
                
            # Normalize score by number of query words
            if query_words:
                score /= len(query_words)
                
            # Bonus for exact word matches
            query_set = set(query_words)
            tool_set = set(tool_words)
            common_words = query_set.intersection(tool_set)
            score += len(common_words) * 0.1
            
            matches.append((tool_name, score))
            
        # Sort by score (highest first)
        matches.sort(key=lambda x: x[1], reverse=True)
        
        # Filter out low-scoring matches
        good_matches = [match for match in matches if match[1] > 0.3]
        
        if not good_matches:
            return False, None, f"No tool found matching '{tool_name_query}'. Available tools: {', '.join(sorted(all_tool_names)[:10])}..."
            
        # Check for a confident match
        best_match, best_score = good_matches[0]
        confidence_threshold = 0.7
        
        if best_score >= confidence_threshold:
            # Check if there are other close matches that might cause ambiguity
            close_matches = [match for match in good_matches[:3] if match[1] >= best_score * 0.8]
            if len(close_matches) > 1:
                names = [match[0] for match in close_matches]
                return False, None, f"Ambiguous tool name '{tool_name_query}'. Did you mean one of: {', '.join(names)}?"
            else:
                return True, best_match, f"Found tool '{best_match}' for query '{tool_name_query}'."
        else:
            # Not confident enough, show top suggestions
            top_matches = [match[0] for match in good_matches[:5]]
            return False, None, f"No confident match for '{tool_name_query}'. Possible matches: {', '.join(top_matches)}."

    async def load_discovered_tools_async(self):
        """Loads all tools discovered in local and global tool directories."""
        if getattr(self.coder, "is_fixing_tool", False):
            return

        if not self.discovered_tool_files:
            return

        self.coder.io.tool_output("Loading custom tools...")
        self.coder.io.tool_output(
            "Note: Tool dependencies will be installed into the same Python environment that Aider"
            " is running in."
        )
        for file_path in self.discovered_tool_files:
            if file_path in self.unloaded_tools:
                continue
            await self.load_tool_async(file_path)

    def _get_local_tools_dir(self):
        if not self.coder.repo:
            return None
        return os.path.join(self.coder.repo.root, ".aider", "tools")

    def _get_global_tools_dir(self):
        return os.path.join(Path.home(), ".aider", "tools")

    def load_dotenv(self, dotenv_path, dotenv_vars):
        if not os.path.exists(dotenv_path):
            return False

        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Remove surrounding quotes
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]

                dotenv_vars[key] = value
        return True

    def load_dot_env_files(self):
        local_tools_dir = self._get_local_tools_dir()
        global_tools_dir = self._get_global_tools_dir()

        dotenv_vars = {}
        loaded_paths = []

        # Load global first
        if global_tools_dir and os.path.isdir(global_tools_dir):
            dotenv_path = os.path.join(global_tools_dir, ".env")
            if self.load_dotenv(dotenv_path, dotenv_vars):
                loaded_paths.append(dotenv_path)

        # Load local, overriding global
        if local_tools_dir and os.path.isdir(local_tools_dir):
            dotenv_path = os.path.join(local_tools_dir, ".env")
            if self.load_dotenv(dotenv_path, dotenv_vars):
                loaded_paths.append(dotenv_path)

        if not dotenv_vars:
            return

        for key, value in dotenv_vars.items():
            if key not in os.environ:
                os.environ[key] = value

        if loaded_paths:
            self.coder.io.tool_output(
                f"Loaded environment variables from: {', '.join(loaded_paths)}"
            )

    def discover_tools(self):
        local_tools_dir = self._get_local_tools_dir()
        global_tools_dir = self._get_global_tools_dir()

        search_dirs = []
        if local_tools_dir and os.path.isdir(local_tools_dir):
            search_dirs.append(local_tools_dir)
        if global_tools_dir and os.path.isdir(global_tools_dir):
            search_dirs.append(global_tools_dir)

        tool_files = []
        for tools_dir in search_dirs:
            for file_name in os.listdir(tools_dir):
                if file_name.endswith(".py") and not file_name.startswith("_"):
                    file_path = os.path.join(tools_dir, file_name)
                    tool_files.append(file_path)
        return tool_files

    async def load_tool_async(self, file_path):
        """Asynchronously loads a tool from a file path, with an option to fix it on failure."""
        if file_path in self.unloaded_tools:
            self.unloaded_tools.remove(file_path)

        while True:
            try:
                module_name = Path(file_path).stem
                if module_name in sys.modules:
                    importlib.reload(sys.modules[module_name])
                    spec = sys.modules[module_name].__spec__
                else:
                    spec = importlib.util.spec_from_file_location(module_name, file_path)

                if not spec or not spec.loader:
                    raise ImportError(f"Could not create spec for module at {file_path}")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Check for new class-based tool format
                if hasattr(module, "Tool") and issubclass(module.Tool, BaseTool):
                    tool_class = module.Tool
                    schema = tool_class.SCHEMA
                    tool_name = schema.get("function", {}).get("name")
                    if tool_name:
                        norm_name = tool_name.lower()
                        self.tools[norm_name] = {
                            "name": tool_name,
                            "definition": schema,
                            "execute": tool_class.execute,
                            "file_path": file_path,
                        }
                        msg = f"Loaded class-based tool '{tool_name}' from {file_path}"
                        self.coder.io.tool_output(msg)
                        return True, msg
                    else:
                        msg = f"Class-based tool '{file_path}' is missing a name in its schema."
                        self.coder.io.tool_warning(msg)
                        return False, msg
                # Check for old function-based tool format
                elif hasattr(module, "get_tool_definition") and hasattr(module, "_execute"):
                    definition = module.get_tool_definition()
                    tool_name = definition.get("function", {}).get("name")
                    if tool_name:
                        norm_name = tool_name.lower()
                        self.tools[norm_name] = {
                            "name": tool_name,
                            "definition": definition,
                            "execute": module._execute,
                            "file_path": file_path,
                        }
                        msg = f"Loaded tool '{tool_name}' from {file_path}"
                        self.coder.io.tool_output(msg)
                        return True, msg
                    else:
                        msg = f"Tool '{file_path}' is missing a name in its definition."
                        self.coder.io.tool_warning(msg)
                        return False, msg
                else:
                    msg = (
                        f"Tool '{file_path}' is not a valid tool file. It must either contain a"
                        " `Tool` class inheriting from `BaseTool` or `get_tool_definition()` and"
                        " `_execute()` functions."
                    )
                    self.coder.io.tool_warning(msg)
                    return False, msg
            except Exception as e:
                # Handle ImportError specifically for missing packages
                if isinstance(e, ImportError):
                    match = re.search(r"No module named '([^']*)'", str(e))
                    package_name = None
                    if match:
                        # get 'requests' from 'requests.exceptions'
                        package_name = match.group(1).split(".")[0]

                    if package_name:
                        res = await self.coder.io.confirm_ask(
                            f"The tool at '{file_path}' failed to load due to a missing package:"
                            f" '{package_name}'.\nDo you want to add it to requirements.txt and"
                            " install it?"
                        )
                        if not res:
                            msg = (
                                "Tool loading aborted. Missing package"
                                f" '{package_name}' was not installed."
                            )
                            self.coder.io.tool_warning(msg)
                            return False, msg

                        # Determine the directory of the tool to find/create requirements.txt
                        tools_dir = os.path.dirname(file_path)
                        requirements_path = os.path.join(tools_dir, "requirements.txt")

                        # Check if package is already in requirements.txt
                        package_already_listed = False
                        if os.path.exists(requirements_path):
                            try:
                                with open(requirements_path, "r") as f:
                                    existing_requirements = f.read().splitlines()
                                # Check if package is already listed (exact match or as a dependency)
                                package_already_listed = any(
                                    req.strip().split("==")[0].split(">=")[0].split("~=")[0]
                                    == package_name
                                    for req in existing_requirements
                                    if req.strip() and not req.startswith("#")
                                )
                            except IOError as io_err:
                                self.coder.io.tool_warning(
                                    f"Failed to read {requirements_path}: {io_err}"
                                )

                        # Add the package to requirements.txt if not already listed
                        if not package_already_listed:
                            try:
                                with open(requirements_path, "a") as f:
                                    f.write(f"{package_name}\n")
                                self.coder.io.tool_output(
                                    f"Added '{package_name}' to {requirements_path}"
                                )
                            except IOError as io_err:
                                error_msg = f"Failed to write to {requirements_path}: {io_err}"
                                self.coder.io.tool_error(error_msg)
                                return False, error_msg
                        else:
                            self.coder.io.tool_output(
                                f"'{package_name}' already listed in {requirements_path}"
                            )

                        # Install the package
                        self.coder.io.tool_output(f"Installing '{package_name}'...")
                        try:
                            subprocess.check_call(
                                [sys.executable, "-m", "pip", "install", package_name]
                            )
                            self.coder.io.tool_output(f"Successfully installed '{package_name}'.")
                            self.coder.io.tool_output(f"Retrying to load tool from {file_path}...")
                            continue  # Retry loading the tool
                        except subprocess.CalledProcessError as install_error:
                            error_msg = (
                                "Failed to install package"
                                f" '{package_name}': {install_error}"
                            )
                            self.coder.io.tool_error(error_msg)
                            # Don't loop, let the user handle it.
                            return False, error_msg
                        except FileNotFoundError:
                            error_msg = (
                                "Failed to install package. `pip` command not found. Is pip"
                                " installed in the current environment?"
                            )
                            self.coder.io.tool_error(error_msg)
                            return False, error_msg

                msg = f"Failed to load tool from {file_path}: {e}"
                self.coder.io.tool_error(msg)

                res = await self.coder.io.confirm_ask(
                    f"The tool at '{file_path}' failed to load. Do you want to try and fix it?"
                )
                if not res:
                    return False, msg

                self.coder.io.tool_output(f"Attempting to fix the failed tool {file_path}...")

                # Add file to context and make it editable
                self.coder.add_rel_fname(file_path)

                # Formulate a prompt for the LLM to fix the file
                try:
                    # Path to the prompt file
                    prompt_path = (
                        Path(__file__).parent / "coders" / "prompts" / "create_tool_prompt.md"
                    )
                    with open(prompt_path, "r") as f:
                        tool_creation_instructions = f.read()
                except FileNotFoundError:
                    tool_creation_instructions = (
                        "Please ensure the tool is a valid Python module with a `Tool` class"
                        " inheriting from `BaseTool`."
                    )

                fix_prompt = (
                    f"{tool_creation_instructions}\n\n"
                    f"The tool at '{file_path}' failed to load with this error:\n"
                    f"```\n{e}\n```\n\n"
                    f"Please fix the code in `{file_path}` according to the instructions above."
                )

                # Temporarily disable pretty output to avoid nested rich.live issues
                original_pretty = self.coder.args.pretty
                try:
                    self.coder.is_fixing_tool = True
                    self.coder.args.pretty = False
                    await self.coder.run(with_message=fix_prompt)
                finally:
                    self.coder.args.pretty = original_pretty
                    self.coder.is_fixing_tool = False
                # Loop will continue and try to load again
            except (EOFError, KeyboardInterrupt):
                raise

    def unload_tool(self, tool_name):
        norm_tool_name = tool_name.lower()
        if norm_tool_name in self.tools:
            tool_info = self.tools[norm_tool_name]
            file_path = tool_info.get("file_path")
            if file_path:
                self.unloaded_tools.add(file_path)

            del self.tools[norm_tool_name]
            self.coder.io.tool_output(f"Unloaded tool '{tool_name}'.")
            return True
        return False

    def list_tools(self):
        return list(self.tools.keys())

    def get_tool_definitions(self):
        return [tool["definition"] for tool in self.tools.values()]

    def delete_tool(self, tool_name):
        norm_tool_name = tool_name.lower()
        if norm_tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found."

        tool_info = self.tools[norm_tool_name]
        file_path = tool_info["file_path"]

        try:
            # First unload the tool to remove it from active tools
            self.unload_tool(tool_name)
            
            # Delete the file from the filesystem
            os.remove(file_path)
            
            # Remove the file path from discovered tools list to prevent future reload attempts
            if file_path in self.discovered_tool_files:
                self.discovered_tool_files.remove(file_path)
                
            # Also remove from unloaded tools list since it's now permanently deleted
            if file_path in self.unloaded_tools:
                self.unloaded_tools.remove(file_path)
                
            return f"Successfully deleted tool '{tool_name}' from {file_path}."
        except Exception as e:
            return f"Error deleting tool file: {e}"

    def move_tool(self, tool_name, scope):
        if tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found."

        tool_info = self.tools[tool_name]
        src_path = tool_info["file_path"]

        if scope == "global":
            dest_dir = self._get_global_tools_dir()
        elif scope == "local":
            dest_dir = self._get_local_tools_dir()
            if not dest_dir:
                return "Error: Cannot move tool to local scope without a git repository."
        else:
            return f"Error: Invalid scope '{scope}'. Must be 'local' or 'global'."

        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(src_path))

        if os.path.exists(dest_path):
            return f"Error: Tool '{os.path.basename(src_path)}' already exists in {scope} scope."

        try:
            os.rename(src_path, dest_path)
            # Update the file path in the tool info
            self.tools[tool_name]["file_path"] = dest_path
            return f"Successfully moved tool '{tool_name}' to {scope} scope."
        except Exception as e:
            return f"Error moving tool: {e}"
