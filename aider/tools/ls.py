import os

from .base_tool import BaseAiderTool


class Ls(BaseAiderTool):
    """
    List files in a directory.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "Ls",
                "description": "List files in a directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "The path to the directory to list.",
                        },
                    },
                    "required": ["directory"],
                },
            },
        }

    def run(self, directory):
        """
        List files in directory and optionally add some to context.

        This provides information about the structure of the codebase,
        similar to how a developer would explore directories.
        """
        try:
            # Make the path relative to root if it's absolute
            if directory.startswith("/"):
                rel_dir = os.path.relpath(directory, self.coder.root)
            else:
                rel_dir = directory

            # Get absolute path
            abs_dir = self.coder.abs_root_path(rel_dir)

            # Check if path exists
            if not os.path.exists(abs_dir):
                self.coder.io.tool_output(f"⚠️ Directory '{directory}' not found")
                return "Directory not found"

            # Get directory contents
            contents = []
            try:
                with os.scandir(abs_dir) as entries:
                    for entry in entries:
                        if entry.is_file() and not entry.name.startswith("."):
                            rel_path = os.path.join(rel_dir, entry.name)
                            contents.append(rel_path)
            except NotADirectoryError:
                # If it's a file, just return the file
                contents = [rel_dir]

            if contents:
                self.coder.io.tool_output(f"📋 Listed {len(contents)} file(s) in '{directory}'")
                if len(contents) > 10:
                    return f"Found {len(contents)} files: {', '.join(contents[:10])}..."
                else:
                    return f"Found {len(contents)} files: {', '.join(contents)}"
            else:
                self.coder.io.tool_output(f"📋 No files found in '{directory}'")
                return "No files found in directory"
        except Exception as e:
            self.coder.io.tool_error(f"Error in ls: {str(e)}")
            return f"Error: {str(e)}"


def execute_ls(coder, dir_path=None, directory=None):
    # Handle both positional and keyword arguments for backward compatibility
    if directory is None and dir_path is not None:
        directory = dir_path
    elif directory is None:
        return "Error: Missing directory parameter"
    return Ls(coder).run(directory=directory)


ls_schema = Ls.get_tool_definition()
