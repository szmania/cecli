from .base_tool import BaseAiderTool


class MakeReadonly(BaseAiderTool):
    """
    Convert an editable file to a read-only file.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "MakeReadonly",
                "description": "Convert an editable file to a read-only file.",
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
        }

    async def run(self, file_path):
        """
        Convert an editable file to a read-only file.

        This allows the LLM to downgrade a file from editable to read-only
        when it determines it no longer needs to make changes to that file.
        """
        try:
            # Get absolute path
            abs_path = self.coder.abs_root_path(file_path)

            # Check if file is in editable context
            if abs_path not in self.coder.abs_fnames:
                if abs_path in self.coder.abs_read_only_fnames:
                    self.coder.io.tool_output(f"📚 File '{file_path}' is already read-only")
                    return "File is already read-only"
                else:
                    self.coder.io.tool_output(f"⚠️ File '{file_path}' not in context")
                    return "File not in context"

            # Move from editable to read-only
            self.coder.abs_fnames.remove(abs_path)
            self.coder.abs_read_only_fnames.add(abs_path)

            self.coder.io.tool_output(f"📚 Made '{file_path}' read-only")
            return "File is now read-only"
        except Exception as e:
            self.coder.io.tool_error(f"Error making file read-only: {str(e)}")
            return f"Error: {str(e)}"


async def _execute_make_readonly(coder, file_path):
    return await MakeReadonly(coder).run(file_path=file_path)


make_readonly_schema = MakeReadonly.get_tool_definition()
