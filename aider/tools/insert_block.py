import re
import traceback

from .base_tool import BaseAiderTool
from .tool_utils import (
    ToolError,
    apply_change,
    find_pattern_indices,
    format_tool_result,
    generate_unified_diff_snippet,
    handle_tool_error,
    select_occurrence_index,
    validate_file_for_edit,
)


class InsertBlock(BaseAiderTool):
    """
    Insert a block of text after or before a specified pattern.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "InsertBlock",
                "description": "Insert a block of text into a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "The path to the file to modify.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The content to insert.",
                        },
                        "after_pattern": {
                            "type": "string",
                            "description": (
                                "Pattern to insert after (mutually exclusive with before_pattern"
                                " and position)."
                            ),
                        },
                        "before_pattern": {
                            "type": "string",
                            "description": (
                                "Pattern to insert before (mutually exclusive with after_pattern"
                                " and position)."
                            ),
                        },
                        "occurrence": {
                            "type": "integer",
                            "description": (
                                "Which occurrence of the pattern to use (1-based, or -1 for last)."
                            ),
                            "default": 1,
                        },
                        "change_id": {
                            "type": "string",
                            "description": "Optional ID for tracking changes.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "If True, only simulate the change.",
                            "default": False,
                        },
                        "position": {
                            "type": "string",
                            "description": (
                                "Special position like 'start_of_file' or 'end_of_file'."
                            ),
                        },
                        "auto_indent": {
                            "type": "boolean",
                            "description": (
                                "If True, automatically adjust indentation of inserted content."
                            ),
                            "default": True,
                        },
                        "use_regex": {
                            "type": "boolean",
                            "description": "If True, treat patterns as regular expressions.",
                            "default": False,
                        },
                    },
                    "required": ["file_path", "content"],
                },
            },
        }

    def run(
        self,
        file_path,
        content,
        after_pattern=None,
        before_pattern=None,
        occurrence=1,
        change_id=None,
        dry_run=False,
        position=None,
        auto_indent=True,
        use_regex=False,
    ):
        """
        Insert a block of text after or before a specified pattern using utility functions.
        """
        tool_name = "InsertBlock"
        try:
            # 1. Validate parameters
            if sum(x is not None for x in [after_pattern, before_pattern, position]) != 1:
                raise ToolError(
                    "Must specify exactly one of: after_pattern, before_pattern, or position"
                )

            # 2. Validate file and get content
            abs_path, rel_path, original_content = validate_file_for_edit(self.coder, file_path)
            lines = original_content.splitlines()

            # Handle empty files
            if not lines:
                lines = [""]

            # 3. Determine insertion point
            insertion_line_idx = 0
            pattern_type = ""
            pattern_desc = ""
            occurrence_str = ""

            if position:
                # Handle special positions
                if position == "start_of_file":
                    insertion_line_idx = 0
                    pattern_type = "at start of"
                elif position == "end_of_file":
                    insertion_line_idx = len(lines)
                    pattern_type = "at end of"
                else:
                    raise ToolError(
                        f"Invalid position: '{position}'. Valid values are 'start_of_file' or"
                        " 'end_of_file'"
                    )
            else:
                # Handle pattern-based insertion
                pattern = after_pattern if after_pattern else before_pattern
                pattern_type = "after" if after_pattern else "before"
                pattern_desc = f"Pattern '{pattern}'"

                # Find pattern matches
                pattern_line_indices = find_pattern_indices(lines, pattern, use_regex=use_regex)

                # Select the target occurrence
                target_line_idx = select_occurrence_index(
                    pattern_line_indices, occurrence, pattern_desc
                )

                # Determine insertion point
                insertion_line_idx = target_line_idx
                if pattern_type == "after":
                    insertion_line_idx += 1  # Insert on the line *after* the matched line

                # Format occurrence info for output
                num_occurrences = len(pattern_line_indices)
                occurrence_str = f"occurrence {occurrence} of " if num_occurrences > 1 else ""

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

            if original_content == new_content:
                self.coder.io.tool_warning("No changes made: insertion would not change file")
                return "Warning: No changes made (insertion would not change file)"

            # 6. Generate diff for feedback
            diff_snippet = generate_unified_diff_snippet(original_content, new_content, rel_path)

            # 7. Handle dry run
            if dry_run:
                if position:
                    dry_run_message = f"Dry run: Would insert block {pattern_type} {file_path}."
                else:
                    dry_run_message = (
                        f"Dry run: Would insert block {pattern_type} {occurrence_str}pattern"
                        f" '{pattern}' in {file_path} at line {insertion_line_idx + 1}."
                    )
                return format_tool_result(
                    self.coder,
                    tool_name,
                    "",
                    dry_run=True,
                    dry_run_message=dry_run_message,
                    diff_snippet=diff_snippet,
                )

            # 8. Apply Change (Not dry run)
            metadata = {
                "insertion_line_idx": insertion_line_idx,
                "after_pattern": after_pattern,
                "before_pattern": before_pattern,
                "position": position,
                "occurrence": occurrence,
                "content": content,
                "auto_indent": auto_indent,
                "use_regex": use_regex,
            }
            final_change_id = apply_change(
                self.coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "insertblock",
                metadata,
                change_id,
            )
            self.coder.files_edited_by_tools.add(rel_path)

            # 9. Format and return result
            if position:
                success_message = f"Inserted block {pattern_type} {file_path}"
            else:
                success_message = (
                    f"Inserted block {pattern_type} {occurrence_str}pattern in {file_path} at line"
                    f" {insertion_line_idx + 1}"
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
            self.coder.io.tool_error(
                f"Error in InsertBlock: {str(e)}\n{traceback.format_exc()}"
            )  # Add traceback
            return f"Error: {str(e)}"


def _execute_insert_block(
    coder,
    file_path,
    content,
    after_pattern=None,
    before_pattern=None,
    occurrence=1,
    change_id=None,
    dry_run=False,
    position=None,
    auto_indent=True,
    use_regex=False,
):
    return InsertBlock(coder).run(
        file_path=file_path,
        content=content,
        after_pattern=after_pattern,
        before_pattern=before_pattern,
        occurrence=occurrence,
        change_id=change_id,
        dry_run=dry_run,
        position=position,
        auto_indent=auto_indent,
        use_regex=use_regex,
    )


insert_block_schema = InsertBlock.get_tool_definition()
