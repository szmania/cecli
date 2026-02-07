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
    NORM_NAME = "deletetext"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "DeleteText",
            "description": "Delete a block of lines from a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "line_number": {"type": "integer"},
                    "line_count": {"type": "integer"},
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
        change_id=None,
        dry_run=False,
        **kwargs,
    ):
        """
        Delete a block of text based on line numbers.
        """
        tool_name = "DeleteText"
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

            # 4. Prepare the deletion
            deleted_lines = lines[start_line : end_line + 1]
            new_lines = lines[:start_line] + lines[end_line + 1 :]
            new_content = "\n".join(new_lines)

            if original_content == new_content:
                coder.io.tool_warning("No changes made: deletion would not change file")
                return "Warning: No changes made (deletion would not change file)"

            # 5. Generate diff for feedback
            num_deleted = end_line - start_line + 1
            basis_desc = f"line {line_number}"

            # 6. Handle dry run
            if dry_run:
                dry_run_message = (
                    f"Dry run: Would delete {num_deleted} lines ({start_line + 1}-{end_line + 1})"
                    f" based on {basis_desc} in {file_path}."
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
                "deleted_content": "\n".join(deleted_lines),
            }
            final_change_id = apply_change(
                coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "deletetext",
                metadata,
                change_id,
            )

            coder.files_edited_by_tools.add(rel_path)
            # 8. Format and return result, adding line range to success message
            success_message = (
                f"Deleted {num_deleted} lines ({start_line + 1}-{end_line + 1}) (from"
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
