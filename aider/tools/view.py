from .base_tool import BaseAiderTool


class View(BaseAiderTool):
    """
    Explicitly add a file to context as read-only.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "View",
                "description": "Explicitly add a file to context as read-only.",
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
        }

    def run(self, file_path):
        """
        Explicitly add a file to context as read-only.

        This gives the LLM explicit control over what files to view,
        rather than relying on indirect mentions.
        """
        try:
            abs_path = self.coder.abs_root_path(file_path)

            if abs_path in self.coder.abs_fnames or abs_path in self.coder.abs_read_only_fnames:
                self.coder.io.tool_output(f"File '{file_path}' is already in context.")
                return "File is already in context."

            if not self.coder.io.confirm_ask(
                "Allow adding new file to chat as read-only?",
                subject=file_path,
            ):
                self.coder.io.tool_output(f"Skipped viewing {file_path}.")
                return "Action skipped by user."

            # Use the coder's helper, marking it as an explicit view request
            return self.coder._add_file_to_context(file_path, explicit=True)
        except Exception as e:
            self.coder.io.tool_error(f"Error viewing file: {str(e)}")
            return f"Error: {str(e)}"


def execute_view(coder, file_path):
    return View(coder).run(file_path=file_path)


view_schema = View.get_tool_definition()
