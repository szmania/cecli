import traceback
from datetime import datetime

from .base_tool import BaseAiderTool


class ListChanges(BaseAiderTool):
    """
    List recent changes made to files.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ListChanges",
                "description": "List recent changes made to files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Optional path to filter changes by file.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of changes to list.",
                            "default": 10,
                        },
                    },
                    "required": [],
                },
            },
        }

    def run(self, file_path=None, limit=10):
        """
        List recent changes made to files.

        Parameters:
        - file_path: Optional path to filter changes by file
        - limit: Maximum number of changes to list

        Returns a formatted list of changes.
        """
        try:
            # If file_path is specified, get the absolute path
            rel_file_path = None
            if file_path:
                abs_path = self.coder.abs_root_path(file_path)
                rel_file_path = self.coder.get_rel_fname(abs_path)

            # Get the list of changes
            changes = self.coder.change_tracker.list_changes(rel_file_path, limit)

            if not changes:
                if file_path:
                    return f"No changes found for file '{file_path}'"
                else:
                    return "No changes have been made yet"

            # Format the changes into a readable list
            result = "Recent changes:\n"
            for i, change in enumerate(changes):
                change_time = datetime.fromtimestamp(change["timestamp"]).strftime("%H:%M:%S")
                change_type = change["type"]
                file_path = change["file_path"]
                change_id = change["id"]

                result += (
                    f"{i + 1}. [{change_id}] {change_time} - {change_type.upper()} on {file_path}\n"
                )

            self.coder.io.tool_output(result)  # Also print to console for user
            return result

        except Exception as e:
            self.coder.io.tool_error(
                f"Error in ListChanges: {str(e)}\n{traceback.format_exc()}"
            )  # Add traceback
            return f"Error: {str(e)}"


def _execute_list_changes(coder, file_path=None, limit=10):
    return ListChanges(coder).run(file_path=file_path, limit=limit)


list_changes_schema = ListChanges.get_tool_definition()
