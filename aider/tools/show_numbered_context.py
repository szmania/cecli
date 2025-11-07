import os

from .base_tool import BaseAiderTool
from .tool_utils import ToolError, handle_tool_error, resolve_paths


class ShowNumberedContext(BaseAiderTool):
    """
    Displays numbered lines from a file centered around a target location.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ShowNumberedContext",
                "description": (
                    "Displays numbered lines from a file centered around a target location"
                    " (pattern or line_number), without adding the file to context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "The path to the file to display.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": (
                                "A pattern to search for to center the context view. Mutually"
                                " exclusive with line_number."
                            ),
                        },
                        "line_number": {
                            "type": "integer",
                            "description": (
                                "A line number to center the context view. Mutually exclusive with"
                                " pattern."
                            ),
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": "The number of lines to show before and after the target.",
                            "default": 3,
                        },
                    },
                    "required": ["file_path"],
                },
            },
        }

    def run(self, file_path, pattern=None, line_number=None, context_lines=3):
        """
        Displays numbered lines from file_path centered around a target location
        (pattern or line_number), without adding the file to context.
        Uses utility functions for path resolution and error handling.
        """
        tool_name = "ShowNumberedContext"
        try:
            # 1. Validate arguments
            if not (pattern is None) ^ (line_number is None):
                raise ToolError("Provide exactly one of 'pattern' or 'line_number'.")

            # 2. Resolve path
            abs_path, rel_path = resolve_paths(self.coder, file_path)
            if not os.path.exists(abs_path):
                # Check existence after resolving, as resolve_paths doesn't guarantee existence
                raise ToolError(f"File not found: {file_path}")

            # 3. Read file content
            content = self.coder.io.read_text(abs_path)
            if content is None:
                raise ToolError(f"Could not read file: {file_path}")
            lines = content.splitlines()
            num_lines = len(lines)

            # 4. Determine center line index
            center_line_idx = -1
            found_by = ""

            if line_number is not None:
                try:
                    line_number_int = int(line_number)
                    if 1 <= line_number_int <= num_lines:
                        center_line_idx = line_number_int - 1  # Convert to 0-based index
                        found_by = f"line {line_number_int}"
                    else:
                        raise ToolError(
                            f"Line number {line_number_int} is out of range (1-{num_lines}) for"
                            f" {file_path}."
                        )
                except ValueError:
                    raise ToolError(f"Invalid line number '{line_number}'. Must be an integer.")

            elif pattern is not None:
                # TODO: Update this section for multiline pattern support later
                first_match_line_idx = -1
                for i, line in enumerate(lines):
                    if pattern in line:
                        first_match_line_idx = i
                        break

                if first_match_line_idx != -1:
                    center_line_idx = first_match_line_idx
                    found_by = f"pattern '{pattern}' on line {center_line_idx + 1}"
                else:
                    raise ToolError(f"Pattern '{pattern}' not found in {file_path}.")

            if center_line_idx == -1:
                # Should not happen if logic above is correct, but as a safeguard
                raise ToolError("Internal error: Could not determine center line.")

            # 5. Calculate context window
            try:
                context_lines_int = int(context_lines)
                if context_lines_int < 0:
                    raise ValueError("Context lines must be non-negative")
            except ValueError:
                self.coder.io.tool_warning(
                    f"Invalid context_lines value '{context_lines}', using default 3."
                )
                context_lines_int = 3

            start_line_idx = max(0, center_line_idx - context_lines_int)
            end_line_idx = min(num_lines - 1, center_line_idx + context_lines_int)

            # 6. Format output
            # Use rel_path for user-facing messages
            output_lines = [f"Displaying context around {found_by} in {rel_path}:"]
            max_line_num_width = len(str(end_line_idx + 1))  # Width for padding

            for i in range(start_line_idx, end_line_idx + 1):
                line_num_str = str(i + 1).rjust(max_line_num_width)
                output_lines.append(f"{line_num_str} | {lines[i]}")

            # Log success and return the formatted context directly
            self.coder.io.tool_output(f"Successfully retrieved context for {rel_path}")
            return "\n".join(output_lines)

        except ToolError as e:
            # Handle expected errors raised by utility functions or validation
            return handle_tool_error(self.coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors during processing
            return handle_tool_error(self.coder, tool_name, e)


def execute_show_numbered_context(
    coder, file_path, pattern=None, line_number=None, context_lines=3
):
    return ShowNumberedContext(coder).run(
        file_path=file_path,
        pattern=pattern,
        line_number=line_number,
        context_lines=context_lines,
    )


show_numbered_context_schema = ShowNumberedContext.get_tool_definition()
