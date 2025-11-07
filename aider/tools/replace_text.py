from .base_tool import BaseAiderTool
from .tool_utils import (
    ToolError,
    apply_change,
    format_tool_result,
    generate_unified_diff_snippet,
    handle_tool_error,
    validate_file_for_edit,
)


class ReplaceText(BaseAiderTool):
    """
    Replace specific text with new text, optionally using nearby context for disambiguation.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ReplaceText",
                "description": (
                    "Replace specific text with new text, optionally using nearby context for"
                    " disambiguation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "The path to the file to modify.",
                        },
                        "find_text": {
                            "type": "string",
                            "description": "The text to find and replace.",
                        },
                        "replace_text": {
                            "type": "string",
                            "description": "The text to replace with.",
                        },
                        "near_context": {
                            "type": "string",
                            "description": (
                                "Optional text nearby to help locate the correct instance of the"
                                " text."
                            ),
                        },
                        "occurrence": {
                            "type": "integer",
                            "description": (
                                "Which occurrence of the text to replace (1-based index, or -1 for"
                                " last)."
                            ),
                            "default": 1,
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
                    "required": ["file_path", "find_text", "replace_text"],
                },
            },
        }

    def run(
        self,
        file_path,
        find_text,
        replace_text,
        near_context=None,
        occurrence=1,
        change_id=None,
        dry_run=False,
    ):
        """
        Replace specific text with new text, optionally using nearby context for disambiguation.
        Uses utility functions for validation, finding occurrences, and applying changes.
        """
        tool_name = "ReplaceText"
        try:
            # 1. Validate file and get content
            abs_path, rel_path, original_content = validate_file_for_edit(self.coder, file_path)

            # 2. Find occurrences using helper function
            # Note: _find_occurrences is currently on the Coder class, not in tool_utils
            occurrences = self.coder._find_occurrences(original_content, find_text, near_context)

            if not occurrences:
                err_msg = f"Text '{find_text}' not found"
                if near_context:
                    err_msg += f" near context '{near_context}'"
                err_msg += f" in file '{file_path}'."
                raise ToolError(err_msg)

            # 3. Select the occurrence index
            num_occurrences = len(occurrences)
            try:
                occurrence = int(occurrence)
                if occurrence == -1:
                    if num_occurrences == 0:
                        raise ToolError(
                            f"Text '{find_text}' not found, cannot select last occurrence."
                        )
                    target_idx = num_occurrences - 1
                elif 1 <= occurrence <= num_occurrences:
                    target_idx = occurrence - 1  # Convert 1-based to 0-based
                else:
                    err_msg = (
                        f"Occurrence number {occurrence} is out of range. Found"
                        f" {num_occurrences} occurrences of '{find_text}'"
                    )
                    if near_context:
                        err_msg += f" near '{near_context}'"
                    err_msg += f" in '{file_path}'."
                    raise ToolError(err_msg)
            except ValueError:
                raise ToolError(f"Invalid occurrence value: '{occurrence}'. Must be an integer.")

            start_index = occurrences[target_idx]

            # 4. Perform the replacement
            new_content = (
                original_content[:start_index]
                + replace_text
                + original_content[start_index + len(find_text) :]
            )

            if original_content == new_content:
                self.coder.io.tool_warning(
                    "No changes made: replacement text is identical to original"
                )
                return "Warning: No changes made (replacement identical to original)"

            # 5. Generate diff for feedback
            # Note: _generate_diff_snippet is currently on the Coder class
            diff_snippet = generate_unified_diff_snippet(
                original_content, new_content, rel_path
            )
            occurrence_str = f"occurrence {occurrence}" if num_occurrences > 1 else "text"

            # 6. Handle dry run
            if dry_run:
                dry_run_message = (
                    f"Dry run: Would replace {occurrence_str} of '{find_text}' in {file_path}."
                )
                return format_tool_result(
                    self.coder,
                    tool_name,
                    "",
                    dry_run=True,
                    dry_run_message=dry_run_message,
                    diff_snippet=diff_snippet,
                )

            # 7. Apply Change (Not dry run)
            metadata = {
                "start_index": start_index,
                "find_text": find_text,
                "replace_text": replace_text,
                "near_context": near_context,
                "occurrence": occurrence,
            }
            final_change_id = apply_change(
                self.coder,
                abs_path,
                rel_path,
                original_content,
                new_content,
                "replacetext",
                metadata,
                change_id,
            )

            self.coder.aider_edited_files.add(rel_path)

            # 8. Format and return result
            success_message = f"Replaced {occurrence_str} in {file_path}"
            return format_tool_result(
                self.coder,
                tool_name,
                success_message,
                change_id=final_change_id,
                diff_snippet=diff_snippet,
            )

        except ToolError as e:
            # Handle errors raised by utility functions or explicitly raised here
            return handle_tool_error(self.coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors
            return handle_tool_error(self.coder, tool_name, e)


def _execute_replace_text(
    coder,
    file_path,
    find_text,
    replace_text,
    near_context=None,
    occurrence=1,
    change_id=None,
    dry_run=False,
):
    return ReplaceText(coder).run(
        file_path=file_path,
        find_text=find_text,
        replace_text=replace_text,
        near_context=near_context,
        occurrence=occurrence,
        change_id=change_id,
        dry_run=dry_run,
    )


replace_text_schema = ReplaceText.get_tool_definition()
