import time

from .base_tool import BaseAiderTool


class Remove(BaseAiderTool):
    """
    Explicitly remove a file from context.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "Remove",
                "description": "Explicitly remove a file from context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "The path to the file to remove from context.",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        }

    def run(self, file_path):
        """
        Explicitly remove a file from context.

        This allows the LLM to clean up its context when files are no
        longer needed, keeping the context focused and efficient.
        """
        try:
            # Get absolute path
            abs_path = self.coder.abs_root_path(file_path)
            rel_path = self.coder.get_rel_fname(abs_path)

            # Check if file is in context (either editable or read-only)
            removed = False
            if abs_path in self.coder.abs_fnames:
                # Don't remove if it's the last editable file and there are no read-only files
                if len(self.coder.abs_fnames) <= 1 and not self.coder.abs_read_only_fnames:
                    self.coder.io.tool_output(
                        f"⚠️ Cannot remove '{file_path}' - it's the only file in context"
                    )
                    return "Cannot remove - last file in context"
                self.coder.abs_fnames.remove(abs_path)
                removed = True
            elif abs_path in self.coder.abs_read_only_fnames:
                # Don't remove if it's the last read-only file and there are no editable files
                if len(self.coder.abs_read_only_fnames) <= 1 and not self.coder.abs_fnames:
                    self.coder.io.tool_output(
                        f"⚠️ Cannot remove '{file_path}' - it's the only file in context"
                    )
                    return "Cannot remove - last file in context"
                self.coder.abs_read_only_fnames.remove(abs_path)
                removed = True

            if not removed:
                self.coder.io.tool_output(f"⚠️ File '{file_path}' not in context")
                return "File not in context"

            # Track in recently removed
            self.coder.recently_removed[rel_path] = {"removed_at": time.time()}

            self.coder.io.tool_output(f"🗑️ Explicitly removed '{file_path}' from context")
            return "Removed file from context"
        except Exception as e:
            self.coder.io.tool_error(f"Error removing file: {str(e)}")
            return f"Error: {str(e)}"


def _execute_remove(coder, file_path):
    return Remove(coder).run(file_path=file_path)


remove_schema = Remove.get_tool_definition()
