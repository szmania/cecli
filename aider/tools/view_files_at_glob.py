import fnmatch
import os

from .base_tool import BaseAiderTool


class ViewFilesAtGlob(BaseAiderTool):
    """
    Execute a glob pattern and add matching files to context as read-only.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ViewFilesAtGlob",
                "description": (
                    "Execute a glob pattern and add matching files to context as read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "The glob pattern to match files against.",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        }

    def run(self, pattern):
        """
        Execute a glob pattern and add matching files to context as read-only.

        This tool helps the LLM find files by pattern matching, similar to
        how a developer would use glob patterns to find files.
        """
        try:
            # Find files matching the pattern
            matching_files = []

            # Make the pattern relative to root if it's absolute
            if pattern.startswith("/"):
                pattern = os.path.relpath(pattern, self.coder.root)

            # Get all files in the repo
            all_files = self.coder.get_all_relative_files()

            # Find matches with pattern matching
            for file in all_files:
                if fnmatch.fnmatch(file, pattern):
                    matching_files.append(file)

            # Limit the number of files added if there are too many matches
            if len(matching_files) > self.coder.max_files_per_glob:
                self.coder.io.tool_output(
                    f"⚠️ Found {len(matching_files)} files matching '{pattern}', "
                    f"limiting to {self.coder.max_files_per_glob} most relevant files."
                )
                # Sort by modification time (most recent first)
                matching_files.sort(
                    key=lambda f: os.path.getmtime(self.coder.abs_root_path(f)), reverse=True
                )
                matching_files = matching_files[: self.coder.max_files_per_glob]

            # Add files to context
            if matching_files:
                # Ask for confirmation for the batch of files
                file_list_preview = ", ".join(matching_files[:5])
                if len(matching_files) > 5:
                    file_list_preview += f", and {len(matching_files) - 5} more"

                if not self.coder.io.confirm_ask(
                    f"Allow adding {len(matching_files)} files matching '{pattern}' to chat as read-only?",
                    subject=file_list_preview,
                ):
                    self.coder.io.tool_output(f"Skipped adding files matching '{pattern}'.")
                    return "Action skipped by user."

                for file in matching_files:
                    # Use the coder's internal method to add files
                    self.coder._add_file_to_context(file)

                # Return a user-friendly result
                if len(matching_files) > 10:
                    brief = ", ".join(matching_files[:5]) + f", and {len(matching_files) - 5} more"
                    self.coder.io.tool_output(
                        f"📂 Added {len(matching_files)} files matching '{pattern}': {brief}"
                    )
                else:
                    self.coder.io.tool_output(
                        f"📂 Added files matching '{pattern}': {', '.join(matching_files)}"
                    )
                return (
                    f"Added {len(matching_files)} files:"
                    f" {', '.join(matching_files[:5])}"
                    f"{' and more' if len(matching_files) > 5 else ''}"
                )
            else:
                self.coder.io.tool_output(f"⚠️ No files found matching '{pattern}'")
                return f"No files found matching '{pattern}'"
        except Exception as e:
            self.coder.io.tool_error(f"Error in ViewFilesAtGlob: {str(e)}")
            return f"Error: {str(e)}"


def execute_view_files_at_glob(coder, pattern):
    return ViewFilesAtGlob(coder).run(pattern=pattern)


view_files_at_glob_schema = ViewFilesAtGlob.get_tool_definition()
