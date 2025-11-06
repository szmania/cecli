import os

from .base_tool import BaseAiderTool
from .tool_utils import (
    ToolError,
    apply_change,
    format_tool_result,
    generate_unified_diff_snippet,
    handle_tool_error,
)


class DeleteLines(BaseAiderTool):
    """
    Delete a range of lines (1-based, inclusive).
    """

    name = "DeleteLines"
    description = "Delete a range of lines in a file."
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path to the file to modify.",
            },
            "start_line": {
                "type": "integer",
                "description": "The 1-based starting line number to delete.",
            },
            "end_line": {
                "type": "integer",
                "description": "The 1-based ending line number to delete.",
            },
            "change_id": {
                "type": "string",
                "description": "Optional ID for tracking the change.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If True, simulate the change without modifying the file.",
                "default": False,
            },
        },
        "required": ["file_path", "start_line", "end_line"],
    }

    def run(self, file_path, start_line, end_line, change_id=None, dry_run=False):
        """
        Delete a range of lines (1-based, inclusive).
        """
        tool_name = "DeleteLines"
        try:
            # Get absolute file path
            abs_path = self.coder.abs_root_path(file_path)
            rel_path = self.coder.get_rel_fname(abs_path)

            # Check if file exists
            if not os.path.isfile(abs_path):
                raise ToolError(f"File '{file_path}' not found")

            # Check if file is in editable context
            if abs_path not in self.coder.abs_fnames:
                if abs_path in self.coder.abs_read_only_fnames:
                    raise ToolError(f"File '{file_path}' is read-only. Use MakeEditable first.")
                else:
                    raise ToolError(f"File '{file_path}' not in context")

            # Reread file content immediately before modification
            file_content = self.coder.io.read_text(abs_path)
            if file_content is None:
                raise ToolError(f"Could not read file '{file_path}'")

            lines = file_content.splitlines()
            original_content = file_content

            # Validate line numbers
            try:
                start_line_int = int(start_line)
                end_line_int = int(end_line)

                if start_line_int < 1 or start_line_int > len(lines):
                    raise ToolError(
                        f"Start line {start_line_int} is out of range (1-{len(lines)})"
                    )
                if end_line_int < 1 or end_line_int > len(lines):
                    raise ToolError(f"End line {end_line_int} is out of range (1-{len(lines)})")
                if start_line_int > end_line_int:
                    raise ToolError(
                        f"Start line {start_line_int} cannot be after end line {end_line_int}"
                    )

                start_idx = start_line_int - 1  # Convert to 0-based index
                end_idx = end_line_int - 1  # Convert to 0-based index
            except ValueError:
                raise ToolError(
                    f"Invalid line numbers: '{start_line}', '{end_line}'. Must be integers."
                )

            # Prepare the deletion
            deleted_lines = lines[start_idx : end_idx + 1]
            new_lines = lines[:start_idx] + lines[end_idx + 1 :]
            new_content = "\n".join(new_lines)

            if original_content == new_content:
                self.coder.io.tool_warning(
                    f"No changes made: deleting lines {start_line_int}-{end_line_int} would not change"
                    " file"
                )
                return (
                    f"Warning: No changes made (deleting lines {start_line_int}-{end_line_int} would"
                    " not change file)"
                )

            # Generate diff snippet
            diff_snippet = generate_unified_diff_snippet(original_content, new_content, rel_path)

            # Handle dry run
            if dry_run:
                dry_run_message = (
                    f"Dry run: Would delete lines {start_line_int}-{end_line_int} in {file_path}"
                )
                return format_tool_result(
                    self.coder,
                    tool_name,
                    "",
                    dry_run=True,
                    dry_run_message=dry_run_message,
                    diff_snippet=diff_snippet,
                )

            # --- Apply Change (Not dry run) ---
            metadata = {
                "start_line": start_line_int,
                "end_line": end_line_int,
                "deleted_content": "\n".join(deleted_lines),
            }

            final_change_id = apply_change(
                self.coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "deletelines",
                metadata,
                change_id,
            )

            self.coder.files_edited_by_tools.add(rel_path)
            num_deleted = end_idx - start_idx + 1
            # Format and return result
            success_message = (
                f"Deleted {num_deleted} lines ({start_line_int}-{end_line_int}) in {file_path}"
            )
            return format_tool_result(
                self.coder,
                tool_name,
                success_message,
                change_id=final_change_id,
                diff_snippet=diff_snippet,
            )

        except ToolError as e:
            # Handle errors raised by utility functions (expected errors)
            return handle_tool_error(self.coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors
            return handle_tool_error(self.coder, tool_name, e)


def _execute_delete_lines(coder, file_path, start_line, end_line, change_id=None, dry_run=False):
    return DeleteLines(coder).run(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        change_id=change_id,
        dry_run=dry_run,
    )


delete_lines_schema = DeleteLines.get_tool_definition()
