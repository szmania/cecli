import difflib
import json

from cecli.tools.utils.base_tool import BaseTool
from cecli.tools.utils.helpers import (
    ToolError,
    apply_change,
    format_tool_result,
    handle_tool_error,
    validate_file_for_edit,
)
from cecli.tools.utils.output import color_markers, tool_footer, tool_header


class Tool(BaseTool):
    NORM_NAME = "replacetext"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "ReplaceText",
            "description": (
                "Replace text in one or more files. Can handle an array of up to 10 edits across"
                " multiple files. Each edit must include its own file_path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {
                                    "type": "string",
                                    "description": "Required file path for this specific edit.",
                                },
                                "find_text": {"type": "string"},
                                "replace_text": {"type": "string"},
                                "line_number": {"type": "integer"},
                                "occurrence": {"type": "integer", "default": 1},
                                "replace_all": {"type": "boolean", "default": False},
                            },
                            "required": ["file_path", "find_text", "replace_text"],
                        },
                        "description": "Array of edits to apply.",
                    },
                    "change_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["edits"],
            },
        },
    }

    @classmethod
    def execute(
        cls,
        coder,
        edits=None,
        change_id=None,
        dry_run=False,
        **kwargs,
    ):
        """
        Replace text in one or more files. Can handle single edit or array of edits across multiple files.
        Each edit object must include its own file_path.
        """
        tool_name = "ReplaceText"
        try:
            # 1. Validate edits parameter
            if not isinstance(edits, list):
                raise ToolError("edits parameter must be an array")

            if len(edits) == 0:
                raise ToolError("edits array cannot be empty")

            # 2. Group edits by file_path
            edits_by_file = {}
            for i, edit in enumerate(edits):
                edit_file_path = edit.get("file_path")
                if edit_file_path is None:
                    raise ToolError(f"Edit {i + 1} missing required file_path parameter")

                if edit_file_path not in edits_by_file:
                    edits_by_file[edit_file_path] = []
                edits_by_file[edit_file_path].append((i, edit))

            # 3. Process each file
            all_results = []
            all_failed_edits = []
            total_successful_edits = 0
            files_processed = 0

            for file_path_key, file_edits in edits_by_file.items():
                try:
                    # Validate file and get content
                    abs_path, rel_path, original_content = validate_file_for_edit(
                        coder, file_path_key
                    )

                    # Process all edits for this file
                    current_content = original_content
                    file_metadata = []
                    file_successful_edits = 0
                    file_failed_edits = []

                    for edit_index, edit in file_edits:
                        try:
                            edit_find_text = edit.get("find_text")
                            edit_replace_text = edit.get("replace_text")
                            edit_line_number = edit.get("line_number")
                            edit_occurrence = edit.get("occurrence", 1)
                            edit_replace_all = edit.get("replace_all", False)

                            if edit_find_text is None or edit_replace_text is None:
                                raise ToolError(
                                    f"Edit {edit_index + 1} missing find_text or replace_text"
                                )

                            # Process this edit
                            new_content, metadata = cls._process_single_edit(
                                coder,
                                file_path_key,
                                edit_find_text,
                                edit_replace_text,
                                edit_line_number,
                                edit_occurrence,
                                current_content,
                                rel_path,
                                abs_path,
                                edit_replace_all,
                            )

                            if metadata is not None:  # Edit made a change
                                current_content = new_content
                                file_metadata.append(metadata)
                                file_successful_edits += 1
                            else:
                                # Edit didn't change anything (identical replacement)
                                file_failed_edits.append(
                                    f"Edit {edit_index + 1}: No change (replacement identical to"
                                    " original)"
                                )

                        except ToolError as e:
                            # Record failed edit but continue with others
                            file_failed_edits.append(f"Edit {edit_index + 1}: {str(e)}")
                            continue

                    # Check if any edits succeeded for this file
                    if file_successful_edits == 0:
                        all_failed_edits.extend(file_failed_edits)
                        continue

                    new_content = current_content

                    # Check if any changes were made for this file
                    if original_content == new_content:
                        all_failed_edits.extend(file_failed_edits)
                        continue

                    # Handle dry run
                    if dry_run:
                        all_results.append(
                            {
                                "file_path": file_path_key,
                                "successful_edits": file_successful_edits,
                                "failed_edits": file_failed_edits,
                                "dry_run": True,
                            }
                        )
                        total_successful_edits += file_successful_edits
                        all_failed_edits.extend(file_failed_edits)
                        files_processed += 1
                        continue

                    # Apply Change (Not dry run)
                    metadata = {
                        "edits": file_metadata,
                        "total_edits": file_successful_edits,
                        "failed_edits": file_failed_edits if file_failed_edits else None,
                    }

                    final_change_id = apply_change(
                        coder,
                        abs_path,
                        rel_path,
                        original_content,
                        new_content,
                        "replacetext",
                        metadata,
                        change_id,
                    )

                    coder.files_edited_by_tools.add(rel_path)

                    all_results.append(
                        {
                            "file_path": file_path_key,
                            "successful_edits": file_successful_edits,
                            "failed_edits": file_failed_edits,
                            "change_id": final_change_id,
                        }
                    )
                    total_successful_edits += file_successful_edits
                    all_failed_edits.extend(file_failed_edits)
                    files_processed += 1

                except ToolError as e:
                    # Record all edits for this file as failed
                    for edit_index, _ in file_edits:
                        all_failed_edits.append(f"Edit {edit_index + 1}: {str(e)}")
                    continue

            # 4. Check if any edits succeeded overall
            if total_successful_edits == 0:
                error_msg = "No edits were successfully applied:\n" + "\n".join(all_failed_edits)
                raise ToolError(error_msg)

            # 5. Handle dry run overall
            if dry_run:
                dry_run_message = (
                    f"Dry run: Would apply {len(edits)} edits across {len(edits_by_file)} files "
                    f"({total_successful_edits} would succeed, {len(all_failed_edits)} would fail)."
                )
                if all_failed_edits:
                    dry_run_message += "\nFailed edits:\n" + "\n".join(all_failed_edits)

                return format_tool_result(
                    coder,
                    tool_name,
                    "",
                    dry_run=True,
                    dry_run_message=dry_run_message,
                )

            # 6. Format and return result
            if files_processed == 1:
                # Single file case for backward compatibility
                result = all_results[0]
                success_message = (
                    f"Applied {result['successful_edits']} edits in {result['file_path']}"
                )
                if result["failed_edits"]:
                    success_message += f" ({len(result['failed_edits'])} failed)"
                change_id_to_return = result.get("change_id")
            else:
                # Multiple files case
                success_message = (
                    f"Applied {total_successful_edits} edits across {files_processed} files"
                )
                if all_failed_edits:
                    success_message += f" ({len(all_failed_edits)} failed)"
                change_id_to_return = None  # Multiple change IDs, can't return single one

            return format_tool_result(
                coder,
                tool_name,
                success_message,
                change_id=change_id_to_return,
            )

        except ToolError as e:
            # Handle errors raised by utility functions or explicitly raised here
            return handle_tool_error(coder, tool_name, e, add_traceback=False)
        except Exception as e:
            # Handle unexpected errors
            return handle_tool_error(coder, tool_name, e)

    @classmethod
    def format_output(cls, coder, mcp_server, tool_response):
        color_start, color_end = color_markers(coder)
        params = json.loads(tool_response.function.arguments)

        tool_header(coder=coder, mcp_server=mcp_server, tool_response=tool_response)

        # Group edits by file_path for display
        edits_by_file = {}

        for i, edit in enumerate(params["edits"]):
            edit_file_path = edit.get("file_path")
            if edit_file_path not in edits_by_file:
                edits_by_file[edit_file_path] = []
            edits_by_file[edit_file_path].append((i, edit))

        # Display edits grouped by file
        for file_path_key, file_edits in edits_by_file.items():
            if file_path_key:
                coder.io.tool_output("")
                coder.io.tool_output(f"{color_start}file_path:{color_end}")
                coder.io.tool_output(file_path_key)
                coder.io.tool_output("")

            for edit_index, edit in file_edits:
                # Show diff for this edit
                diff = difflib.unified_diff(
                    edit.get("find_text", "").splitlines(),
                    edit.get("replace_text", "").splitlines(),
                    lineterm="",
                    n=float("inf"),
                )
                diff_lines = list(diff)[2:]  # Skip header lines
                if diff_lines:
                    if len(params["edits"]) > 1:
                        coder.io.tool_output(f"{color_start}diff_{edit_index + 1}:{color_end}")
                    else:
                        coder.io.tool_output(f"{color_start}diff:{color_end}")

                    coder.io.tool_output("\n".join([line for line in diff_lines]))
                    coder.io.tool_output("")

        tool_footer(coder=coder, tool_response=tool_response)

    @classmethod
    def _process_single_edit(
        cls,
        coder,
        file_path,
        find_text,
        replace_text,
        line_number=None,
        occurrence=1,
        original_content=None,
        rel_path=None,
        abs_path=None,
        replace_all=False,
    ):
        """
        Process a single edit and return the modified content and metadata.
        """
        # Find all occurrences of the text in the file
        occurrence_indices = coder._find_occurrences(original_content, find_text, None)

        if not occurrence_indices:
            err_msg = f"Text '{find_text}' not found in file '{file_path}'."
            raise ToolError(err_msg)

        # Handle replace_all case
        if replace_all:
            # Replace all occurrences
            new_content = original_content
            replaced_count = 0

            # Need to process from end to beginning to maintain correct indices
            for idx in reversed(occurrence_indices):
                new_content = new_content[:idx] + replace_text + new_content[idx + len(find_text) :]
                replaced_count += 1

            if original_content == new_content:
                return original_content, None  # No change

            metadata = {
                "start_index": occurrence_indices[0] if occurrence_indices else None,
                "find_text": find_text,
                "replace_text": replace_text,
                "line_number": line_number,
                "occurrence": -1,  # Special value indicating all occurrences
                "replaced_count": replaced_count,
            }

            return new_content, metadata

        # Original logic for single occurrence replacement
        # If line_number is provided, find the occurrence closest to that line
        if line_number is not None:
            try:
                line_number = int(line_number)
                # Validate line number is within file bounds
                lines = original_content.splitlines(keepends=True)
                if line_number < 1 or line_number > len(lines):
                    raise ToolError(
                        f"Line number {line_number} is out of range. File has {len(lines)} lines."
                    )

                # Calculate which line each occurrence is on
                occurrence_lines = []
                for occ_idx in occurrence_indices:
                    # Count newlines before this occurrence to determine line number
                    lines_before = original_content[:occ_idx].count("\n")
                    line_num = lines_before + 1  # Convert to 1-based line numbering
                    occurrence_lines.append((occ_idx, line_num))

                # Find the occurrence on or after the specified line number
                # If none found, use the last occurrence before the line number
                target_idx = None
                min_distance_after = float("inf")
                last_before_idx = None
                last_before_distance = float("inf")

                for i, (occ_idx, occ_line) in enumerate(occurrence_lines):
                    distance = occ_line - line_number

                    if distance >= 0:  # On or after the line number
                        if distance < min_distance_after:
                            min_distance_after = distance
                            target_idx = i
                    else:  # Before the line number
                        if abs(distance) < last_before_distance:
                            last_before_distance = abs(distance)
                            last_before_idx = i

                # If no occurrence on or after, use the closest before
                if target_idx is None and last_before_idx is not None:
                    target_idx = last_before_idx

                if target_idx is None:
                    raise ToolError(f"No occurrence of '{find_text}' found in file '{file_path}'.")

                selected_occurrence = (
                    1  # We're selecting based on line_number, so occurrence is always 1
                )

            except ValueError:
                raise ToolError(f"Invalid line number: '{line_number}'. Must be an integer.")
        else:
            # No line_number specified, use the occurrence parameter
            num_occurrences = len(occurrence_indices)
            try:
                occurrence = int(occurrence)
                if occurrence == -1:
                    if num_occurrences == 0:
                        raise ToolError(
                            f"Text '{find_text}' not found, cannot select last occurrence."
                        )
                    target_idx = num_occurrences - 1
                    selected_occurrence = occurrence
                elif 1 <= occurrence <= num_occurrences:
                    target_idx = occurrence - 1  # Convert 1-based to 0-based
                    selected_occurrence = occurrence
                else:
                    err_msg = (
                        f"Occurrence number {occurrence} is out of range. Found"
                        f" {num_occurrences} occurrences of '{find_text}' in '{file_path}'."
                    )
                    raise ToolError(err_msg)
            except ValueError:
                raise ToolError(f"Invalid occurrence value: '{occurrence}'. Must be an integer.")

        start_index = occurrence_indices[target_idx]

        # Perform the replacement
        new_content = (
            original_content[:start_index]
            + replace_text
            + original_content[start_index + len(find_text) :]
        )

        if original_content == new_content:
            return original_content, None  # No change

        metadata = {
            "start_index": start_index,
            "find_text": find_text,
            "replace_text": replace_text,
            "line_number": line_number,
            "occurrence": selected_occurrence,
        }

        return new_content, metadata
