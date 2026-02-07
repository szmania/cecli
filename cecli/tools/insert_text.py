import re
import traceback

from cecli.tools.utils.base_tool import BaseTool
from cecli.tools.utils.helpers import (
    ToolError,
    apply_change,
    format_tool_result,
    handle_tool_error,
    is_provided,
    validate_file_for_edit,
)
from cecli.tools.utils.output import tool_body_unwrapped, tool_footer, tool_header


class Tool(BaseTool):
    NORM_NAME = "inserttext"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "InsertText",
            "description": (
                "Insert a content into a file. Mutually Exclusive Parameters:"
                " position, line_number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                    "line_number": {"type": "integer"},
                    "change_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "position": {"type": "string", "enum": ["top", "bottom", ""]},
                    "auto_indent": {"type": "boolean", "default": True},
                },
                "required": ["file_path", "content"],
            },
        },
    }

    @classmethod
    def execute(
        cls,
        coder,
        file_path,
        content,
        line_number=None,
        change_id=None,
        dry_run=False,
        position=None,
        auto_indent=True,
        **kwargs,
    ):
        """
        Insert a block of text at a line number or special position.

        Args:
            coder: The coder instance
            file_path: Path to the file to modify
            content: The content to insert
            line_number: Line number to insert at (1-based, mutually exclusive with position)
            change_id: Optional ID for tracking changes
            dry_run: If True, only simulate the change
            position: Special position like "top" or "bottom" (mutually exclusive with line_number)
            auto_indent: If True, automatically adjust indentation of inserted content
        """
        tool_name = "InsertText"
        try:
            # 1. Validate parameters
            if sum(is_provided(x) for x in [position, line_number]) != 1:
                # Check if file is empty or contains only whitespace
                abs_path, rel_path, original_content = validate_file_for_edit(coder, file_path)
                if not original_content.strip():
                    # File is empty or contains only whitespace, default to inserting at beginning
                    position = "top"
                else:
                    raise ToolError("Must specify exactly one of: position or line_number")

            # 2. Validate file and get content
            abs_path, rel_path, original_content = validate_file_for_edit(coder, file_path)
            lines = original_content.splitlines()

            # Handle empty files
            if not lines:
                lines = [""]

            # 3. Determine insertion point
            insertion_line_idx = 0
            pattern_type = ""

            if position:
                # Handle special positions
                if position == "start_of_file" or position == "top":
                    insertion_line_idx = 0
                    pattern_type = "at start of"
                elif position == "end_of_file" or position == "bottom":
                    insertion_line_idx = len(lines)
                    pattern_type = "at end of"
                else:
                    raise ToolError(
                        f"Invalid position: '{position}'. Valid values are 'start_of_file' or"
                        " 'end_of_file'"
                    )
            else:
                # Handle line number insertion (1-based)
                if line_number < 1:
                    insertion_line_idx = 0
                elif line_number > len(lines) + 1:
                    insertion_line_idx = len(lines)
                else:
                    insertion_line_idx = line_number - 1
                pattern_type = "at line"

            # 4. Handle indentation if requested
            content_lines = content.splitlines()

            if auto_indent and content_lines:
                # Determine base indentation level
                base_indent = ""
                if insertion_line_idx > 0 and lines:
                    # Use indentation from the line before insertion point
                    reference_line_idx = min(insertion_line_idx - 1, len(lines) - 1)
                    reference_line = lines[reference_line_idx]
                    base_indent = re.match(r"^(\s*)", reference_line).group(1)

                # Apply indentation to content lines, preserving relative indentation
                if content_lines:
                    # Find minimum indentation in content to preserve relative indentation
                    content_indents = [
                        len(re.match(r"^(\s*)", line).group(1))
                        for line in content_lines
                        if line.strip()
                    ]
                    min_content_indent = min(content_indents) if content_indents else 0

                    # Apply base indentation while preserving relative indentation
                    indented_content_lines = []
                    for line in content_lines:
                        if not line.strip():  # Empty or whitespace-only line
                            indented_content_lines.append("")
                        else:
                            # Remove existing indentation and add new base indentation
                            stripped_line = (
                                line[min_content_indent:]
                                if min_content_indent <= len(line)
                                else line
                            )
                            indented_content_lines.append(base_indent + stripped_line)

                    content_lines = indented_content_lines

            # 5. Prepare the insertion
            new_lines = lines[:insertion_line_idx] + content_lines + lines[insertion_line_idx:]
            new_content = "\n".join(new_lines)

            # Restore trailing newline if original file had one
            if original_content.endswith("\n"):
                new_content += "\n"

            if original_content == new_content:
                coder.io.tool_warning("No changes made: insertion would not change file")
                return "Warning: No changes made (insertion would not change file)"

            # 6. Handle dry run
            if dry_run:
                if position:
                    dry_run_message = f"Dry run: Would insert block {pattern_type} {file_path}."
                else:
                    dry_run_message = (
                        f"Dry run: Would insert block {pattern_type} {line_number} in {file_path}."
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
                "insertion_line_idx": insertion_line_idx,
                "line_number": line_number,
                "position": position,
                "content": content,
                "auto_indent": auto_indent,
            }
            final_change_id = apply_change(
                coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "inserttext",
                metadata,
                change_id,
            )

            coder.files_edited_by_tools.add(rel_path)

            # 8. Format and return result
            if position:
                success_message = f"Inserted block {pattern_type} {file_path}"
            else:
                success_message = f"Inserted block {pattern_type} {line_number} in {file_path}"

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
            coder.io.tool_error(
                f"Error in InsertText: {str(e)}\n{traceback.format_exc()}"
            )  # Add traceback
            return f"Error: {str(e)}"

    @classmethod
    def format_output(cls, coder, mcp_server, tool_response):
        tool_header(coder=coder, mcp_server=mcp_server, tool_response=tool_response)
        tool_body_unwrapped(coder=coder, tool_response=tool_response)
        tool_footer(coder=coder, tool_response=tool_response)
