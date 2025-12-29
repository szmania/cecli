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
        self.attempted_installs = set()  # Track package installation attempts
        self.skip_all_installs = False
        self.skip_all_fixes = False
        self.local_tools_python = None
        self.global_tools_python = None
        self._setup_virtual_environments()
        self.load_dot_env_files()
        self.discovered_tool_files = self.discover_tools()

    def find_tool(self, tool_name_query, search_scope='all'):
        """
        Find a tool by fuzzy matching the tool name query.
        
        Args:
            tool_name_query (str): The user's tool name query
            search_scope (str): Scope to search - 'all', 'custom', or 'standard'
            
        Returns:
            tuple: (success: bool, tool_name: str, message: str)
                   - If success is True, tool_name contains the matched tool name
                   - If success is False, message contains error information
        """
        if not tool_name_query:
            return False, None, "No tool name provided."
            
        # Get available tool names based on scope
        all_tool_names = set()
        
        # Add custom tool names if in scope
        if search_scope in ['all', 'custom']:
            for tool_info in self.tools.values():
                all_tool_names.add(tool_info["name"])
            
        # Add standard tool names if in scope
        if search_scope in ['all', 'standard']:
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
            scope_msg = ""
            if search_scope == 'custom':
                scope_msg = " No custom tools available."
            elif search_scope == 'standard':
                scope_msg = " No standard tools available."
            return False, None, "No tools available." + scope_msg
            
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
            "Note: Tool dependencies will be installed into isolated virtual environments."
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

    def _get_or_create_tools_venv(self, scope):
        """Create or retrieve the virtual environment for tools of a given scope."""
        if scope == "local":
            tools_dir = self._get_local_tools_dir()
        elif scope == "global":
            tools_dir = self._get_global_tools_dir()
        else:
            raise ValueError(f"Invalid scope: {scope}")

        if not tools_dir:
            return None

        venv_path = os.path.join(tools_dir, "venv")
        
        # Create virtual environment if it doesn't exist
        if not os.path.exists(venv_path):
            self.coder.io.tool_output(f"Creating isolated environment for {scope} tools...")
            try:
                subprocess.run([sys.executable, "-m", "venv", venv_path], check=True)
            except subprocess.CalledProcessError as e:
                self.coder.io.tool_error(f"Failed to create virtual environment for {scope} tools: {e}")
                return None

        # Determine the Python executable path based on the platform
        if sys.platform == "win32":
            python_executable = os.path.join(venv_path, "Scripts", "python.exe")
        else:
            python_executable = os.path.join(venv_path, "bin", "python")
            
        # Add the site-packages directory to sys.path
        if sys.platform == "win32":
            site_packages = os.path.join(venv_path, "Lib", "site-packages")
        else:
            # For Unix-like systems, we need to account for Python version
            python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site_packages = os.path.join(venv_path, "lib", python_version, "site-packages")
            
        if site_packages not in sys.path:
            sys.path.insert(0, site_packages)
            
        return python_executable

    def _setup_virtual_environments(self):
        """Set up virtual environments for both local and global tools."""
        self.local_tools_python = self._get_or_create_tools_venv("local")
        self.global_tools_python = self._get_or_create_tools_venv("global")

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
                    raise ValueError(msg)
            except Exception as e:
                # Handle ImportError specifically for missing packages
                if isinstance(e, ImportError):
                    match = re.search(r"No module named '([^']*)'", str(e))
                    package_name = None
                    if match:
                        # get 'requests' from 'requests.exceptions'
                        package_name = match.group(1).split(".")[0]

                    if package_name and package_name != "aider":
                        if self.skip_all_installs:
                            msg = f"Skipping installation of '{package_name}' due to user request."
                            self.coder.io.tool_warning(msg)
                            self.unloaded_tools.add(file_path)
                            return False, msg

                        # Check if we've already tried to install this package for this tool
                        install_key = (file_path, package_name)
                        if install_key in self.attempted_installs:
                            # We've already tried installing this package, skip to fix prompt
                            pass
                        else:
                            # Track this installation attempt
                            self.attempted_installs.add(install_key)

                            res = await self.coder.io.confirm_ask(
                                f"The tool at '{file_path}' failed to load due to a missing package:"
                                f" '{package_name}'.\nDo you want to install it in the appropriate"
                                " virtual environment?",
                                allow_never=True,
                                allow_skip_all=True,
                            )
                            if res == "skip_all":
                                self.skip_all_installs = True
                                res = False

                            if not res:
                                msg = (
                                    "Tool loading aborted. Missing package"
                                    f" '{package_name}' was not installed."
                                )
                                self.coder.io.tool_warning(msg)
                                # Add to unloaded tools to prevent further attempts
                                self.unloaded_tools.add(file_path)
                                return False, msg

                            # Determine if the tool is local or global
                            local_tools_dir = self._get_local_tools_dir()
                            global_tools_dir = self._get_global_tools_dir()
                            
                            if local_tools_dir and file_path.startswith(local_tools_dir):
                                scope = "local"
                                python_executable = self.local_tools_python
                            elif global_tools_dir and file_path.startswith(global_tools_dir):
                                scope = "global"
                                python_executable = self.global_tools_python
                            else:
                                # Fallback to current environment
                                python_executable = sys.executable
                                scope = "current"

                            # Install the package in the appropriate environment
                            self.coder.io.tool_output(f"Installing '{package_name}' in {scope} environment...")
                            try:
                                # Use the correct Python executable for the tool's scope
                                if scope == "local":
                                    python_exe = self.local_tools_python
                                elif scope == "global":
                                    python_exe = self.global_tools_python
                                else:
                                    python_exe = sys.executable  # Fallback

                                subprocess.check_call(
                                    [python_exe, "-m", "pip", "install", package_name]
                                )
                                self.coder.io.tool_output(f"Successfully installed '{package_name}' in {scope} environment.")
                                self.coder.io.tool_output(f"Retrying to load tool from {file_path}...")
                                continue  # Retry loading the tool
                            except subprocess.CalledProcessError as install_error:
                                error_msg = (
                                    f"Failed to install package '{package_name}' in {scope} environment: {install_error}"
                                )
                                self.coder.io.tool_error(error_msg)
                                # Don't loop, let the user handle it.
                                return False, error_msg
                            except FileNotFoundError:
                                error_msg = (
                                    f"Failed to install package in {scope} environment. `pip` command not found."
                                )
                                self.coder.io.tool_error(error_msg)
                                return False, error_msg

                msg = f"Failed to load tool from {file_path}: {e}"
                self.coder.io.tool_error(msg)
                if self.skip_all_fixes:
                    msg = f"Skipping fix for '{file_path}' due to user request."
                    self.coder.io.tool_warning(msg)
                    self.unloaded_tools.add(file_path)
                    return False, msg

                res = await self.coder.io.confirm_ask(
                    f"The tool at '{file_path}' failed to load. Do you want to try and fix it?",
                    allow_never=True,
                    allow_skip_all=True,
                )
                if res == "skip_all":
                    self.skip_all_fixes = True
                    res = False

                if not res:
                    # Add to unloaded tools to prevent further attempts
                    self.unloaded_tools.add(file_path)
                    return False, msg

                self.coder.io.tool_output(f"Attempting to fix the failed tool {file_path}...")

                # Add file to context and make it editable
                await self.coder.commands.cmd_add(f'"{file_path}"', is_tool_file=True)

                # Formulate a prompt for the LLM to fix the file
                fix_prompt = (
                    f"The tool file `{file_path}` is in the chat context, but it failed to load with the following error:\n"
                    f"```\n{e}\n```\n\n"
                    "Please fix the code in the file. The most likely cause is an incorrect import or class structure. "
                    "Ensure the tool inherits from `aider.tools.utils.base_tool.BaseTool` and has the correct `SCHEMA` and `execute` method structure. "
                    "Since the entire file is in context, you must replace the whole file content. "
                    "To do this, you MUST use a `SEARCH/REPLACE` block with the following exact syntax. Do not wrap it in a code fence.\n\n"
                    f"{file_path}\n"
                    "<<<<<<< SEARCH\n"
                    "<the entire original content of the file>\n"
                    "=======\n"
                    "<the entire new content of the file>\n"
                    ">>>>>>> REPLACE\n\n"
                    "Do not use any other editing method. "
                    "If you are unable to fix the file, please explain why and do not attempt to use any other tools."
                )

                # Temporarily disable pretty output and preprocessing to avoid nested rich.live issues and loops
                original_pretty = self.coder.args.pretty
                try:
                    self.coder.is_fixing_tool = True
                    self.coder.args.pretty = False
                    # Use run_one with preproc=False to avoid implicit file adds and loops
                    await self.coder.run_one(user_message=fix_prompt, preproc=False)
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

    def remove_tool(self, tool_name):
        norm_tool_name = tool_name.lower()
        if norm_tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found."

        tool_info = self.tools[norm_tool_name]
        file_path = tool_info.get("file_path")

        if not file_path or not os.path.exists(file_path):
            # If there's no file path, we can still unload it
            self.unload_tool(tool_name)
            return f"Warning: Tool '{tool_name}' had no associated file to delete, but was unloaded."

        try:
            os.remove(file_path)
            # unload_tool also removes the tool from the self.tools dictionary
            self.unload_tool(tool_name)
            return f"Successfully deleted tool '{tool_name}' and its file."
        except Exception as e:
            return f"Error deleting tool file '{file_path}': {e}"

    def list_tools(self):
        return list(self.tools.keys())

    def get_tool_definitions(self):
        return [tool["definition"] for tool in self.tools.values()]

    def move_tool(self, tool_name, scope):
        norm_tool_name = tool_name.lower()
        if norm_tool_name not in self.tools:
            return f"Error: Tool '{tool_name}' not found."

        tool_info = self.tools[norm_tool_name]
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
            self.tools[norm_tool_name]["file_path"] = dest_path
            return f"Successfully moved tool '{tool_name}' to {scope} scope."
        except Exception as e:
            return f"Error moving tool: {e}"
