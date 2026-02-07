from cecli.tools.utils.base_tool import BaseTool
from cecli.tools.utils.helpers import (
    ToolError,
    apply_change,
    determine_line_range,
    format_tool_result,
    handle_tool_error,
    validate_file_for_edit,
)


class Tool(BaseTool):
    NORM_NAME = "indenttext"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "IndentText",
            "description": "Indent lines in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "line_number": {"type": "integer"},
                    "line_count": {"type": "integer"},
                    "indent_levels": {"type": "integer", "default": 1},
                    "change_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["file_path", "line_number"],
            },
        },
    }

    @classmethod
    def execute(
        cls,
        coder,
        file_path,
        line_number,
        line_count=None,
        indent_levels=1,
        change_id=None,
        dry_run=False,
        **kwargs,
    ):
        """
        Indent or unindent a block of lines in a file.

        Parameters:
        - coder: The Coder instance
        - file_path: Path to the file to modify
        - line_number: Line number to start indenting from (1-based)
        - line_count: Optional number of lines to indent
        - indent_levels: Number of levels to indent (positive) or unindent (negative)
        - change_id: Optional ID for tracking the change
        - dry_run: If True, simulate the change without modifying the file

        Returns a result message.
        """
        tool_name = "IndentText"
        try:
            # 1. Validate file and get content
            abs_path, rel_path, original_content = validate_file_for_edit(coder, file_path)
            lines = original_content.splitlines()

            # 2. Determine the range
            start_line_idx = line_number - 1
            pattern_desc = f"line {line_number}"

            start_line, end_line = determine_line_range(
                coder=coder,
                file_path=rel_path,
                lines=lines,
                start_pattern_line_index=start_line_idx,
                end_pattern=None,
                line_count=line_count,
                target_symbol=None,
                pattern_desc=pattern_desc,
            )

            # 4. Validate and prepare indentation
            try:
                indent_levels = int(indent_levels)
            except ValueError:
                raise ToolError(
                    f"Invalid indent_levels value: '{indent_levels}'. Must be an integer."
                )

            indent_str = " " * 4  # Assume 4 spaces per level
            modified_lines = list(lines)

            # Apply indentation logic (core logic remains)
            for i in range(start_line, end_line + 1):
                if indent_levels > 0:
                    modified_lines[i] = (indent_str * indent_levels) + modified_lines[i]
                elif indent_levels < 0:
                    spaces_to_remove = abs(indent_levels) * len(indent_str)
                    current_leading_spaces = len(modified_lines[i]) - len(
                        modified_lines[i].lstrip(" ")
                    )
                    actual_remove = min(spaces_to_remove, current_leading_spaces)
                    if actual_remove > 0:
                        modified_lines[i] = modified_lines[i][actual_remove:]

            new_content = "\n".join(modified_lines)

            if original_content == new_content:
                coder.io.tool_warning("No changes made: indentation would not change file")
                return "Warning: No changes made (indentation would not change file)"

            # 5. Generate diff for feedback
            action = "indent" if indent_levels > 0 else "unindent"
            levels = abs(indent_levels)
            level_text = "level" if levels == 1 else "levels"
            num_lines = end_line - start_line + 1
            basis_desc = f"line {line_number}"

            # 6. Handle dry run
            if dry_run:
                dry_run_message = (
                    f"Dry run: Would {action} {num_lines} lines ({start_line + 1}-{end_line + 1})"
                    f" by {levels} {level_text} (based on {basis_desc}) in {file_path}."
                )
                return format_tool_result(
                    coder,
                    tool_name,
                    "",
                    dry_run=True,
                    dry_run_message=dry_run_message,
                )

            # 7. Apply Change (Not dry run)
            metadata = {
                "start_line": start_line + 1,
                "end_line": end_line + 1,
                "line_number": line_number,
                "line_count": line_count,
                "indent_levels": indent_levels,
            }
            final_change_id = apply_change(
                coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "indenttext",
                metadata,
                change_id,
            )

            coder.files_edited_by_tools.add(rel_path)

            # 8. Format and return result
            action_past = "Indented" if indent_levels > 0 else "Unindented"
            success_message = (
                f"{action_past} {num_lines} lines by {levels} {level_text} (from"
                f" {basis_desc}) in {file_path}"
            )
            return format_tool_result(
                coder,
                tool_name,
                success_message,
                change_id=final_change_id,
            )
        except ToolError as e:
            # Handle errors raised by utility functions (expected errors)
            return handle_tool_error(coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors
            return handle_tool_error(coder, tool_name, e)
