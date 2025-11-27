import os
import sys
import importlib.util
from pathlib import Path

from aider.tools.utils.base_tool import BaseTool


class ToolManager:
    def __init__(self, coder):
        self.coder = coder
        self.tools = {}
        self.load_dot_env_files()
        self.discovered_tool_files = self.discover_tools()
        self.load_discovered_tools()

    def load_discovered_tools(self):
        """Loads all tools discovered in local and global tool directories."""
        if not self.discovered_tool_files:
            return

        self.coder.io.tool_output("Loading custom tools...")
        for file_path in self.discovered_tool_files:
            self.load_tool(file_path)

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

    def load_tool(self, file_path):
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
                    self.tools[tool_name] = {
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
                    self.tools[tool_name] = {
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
            msg = f"Failed to load tool from {file_path}: {e}"
            self.coder.io.tool_error(msg)
            return False, msg

    def unload_tool(self, tool_name):
        if tool_name in self.tools:
            del self.tools[tool_name]
            self.coder.io.tool_output(f"Unloaded tool '{tool_name}'.")
            return True
        return False

    def list_tools(self):
        return list(self.tools.keys())

    def get_tool_definitions(self):
        return [tool["definition"] for tool in self.tools.values()]

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
