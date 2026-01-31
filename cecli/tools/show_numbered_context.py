import os

from cecli.tools.utils.base_tool import BaseTool
from cecli.tools.utils.helpers import (
    ToolError,
    handle_tool_error,
    is_provided,
    resolve_paths,
)


class Tool(BaseTool):
    NORM_NAME = "shownumberedcontext"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "ShowNumberedContext",
            "description": (
                "Show numbered lines of context around patterns or line numbers in multiple files."
                " Accepts an array of show objects, each with file_path, pattern/line_number, and"
                " context_lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "show": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {
                                    "type": "string",
                                    "description": "File path to search in.",
                                },
                                "pattern": {
                                    "type": "string",
                                    "description": (
                                        "Pattern to search for (mutually exclusive with"
                                        " line_number)."
                                    ),
                                },
                                "line_number": {
                                    "type": "integer",
                                    "description": (
                                        "Line number to show context around (mutually exclusive"
                                        " with pattern)."
                                    ),
                                },
                                "context_lines": {
                                    "type": "integer",
                                    "default": 3,
                                    "description": (
                                        "Number of context lines to show around the target."
                                    ),
                                },
                            },
                            "required": ["file_path"],
                        },
                        "description": "Array of show operations to perform.",
                    },
                },
                "required": ["show"],
            },
        },
    }

    @classmethod
    def execute(cls, coder, show, **kwargs):
        """
        Displays numbered lines from multiple files centered around target locations
        (patterns or line_numbers), without adding files to context.
        Accepts an array of show operations to perform.
        Uses utility functions for path resolution and error handling.
        """
        tool_name = "ShowNumberedContext"
        try:
            # 1. Validate show parameter
            if not isinstance(show, list):
                raise ToolError("show parameter must be an array")

            if len(show) == 0:
                raise ToolError("show array cannot be empty")

            all_outputs = []

            for show_index, show_op in enumerate(show):
                # Extract parameters for this show operation
                file_path = show_op.get("file_path")
                pattern = show_op.get("pattern")
                line_number = show_op.get("line_number")
                context_lines = show_op.get("context_lines", 3)

                if file_path is None:
                    raise ToolError(
                        f"Show operation {show_index + 1} missing required file_path parameter"
                    )

                # Validate arguments for this operation
                pattern_provided = is_provided(pattern)
                line_number_provided = is_provided(line_number, treat_zero_as_missing=True)

                if sum([pattern_provided, line_number_provided]) != 1:
                    raise ToolError(
                        f"Show operation {show_index + 1}: Provide exactly one of 'pattern' or"
                        " 'line_number'."
                    )

                if not pattern_provided:
                    pattern = None
                if not line_number_provided:
                    line_number = None

                # 2. Resolve path
                abs_path, rel_path = resolve_paths(coder, file_path)
                if not os.path.exists(abs_path):
                    # Check existence after resolving, as resolve_paths doesn't guarantee existence
                    raise ToolError(f"File not found: {file_path}")

                # 3. Read file content
                content = coder.io.read_text(abs_path)
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
                    coder.io.tool_warning(
                        f"Invalid context_lines value '{context_lines}', using default 3."
                    )
                    context_lines_int = 3

                start_line_idx = max(0, center_line_idx - context_lines_int)
                end_line_idx = min(num_lines - 1, center_line_idx + context_lines_int)

                # 6. Format output for this operation
                # Use rel_path for user-facing messages
                output_lines = [f"Displaying context around {found_by} in {rel_path}:"]
                max_line_num_width = len(str(end_line_idx + 1))  # Width for padding

                for i in range(start_line_idx, end_line_idx + 1):
                    line_num_str = str(i + 1).rjust(max_line_num_width)
                    output_lines.append(f"{line_num_str} | {lines[i]}")

                # Add separator between multiple show operations
                if show_index > 0:
                    all_outputs.append("")
                all_outputs.extend(output_lines)

            # Log success and return the formatted context directly
            coder.io.tool_output(f"Successfully retrieved context for {len(show)} file(s)")
            return "\n".join(all_outputs)

        except ToolError as e:
            # Handle expected errors raised by utility functions or validation
            return handle_tool_error(coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors during processing
            return handle_tool_error(coder, tool_name, e)
