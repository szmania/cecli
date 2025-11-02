import fnmatch
import re

from .base_tool import BaseAiderTool


class ViewFilesMatching(BaseAiderTool):
    """
    Search for a pattern in files and add matching files to context as read-only.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ViewFilesMatching",
                "description": (
                    "Search for a pattern in files and add matching files to context as read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search_pattern": {
                            "type": "string",
                            "description": (
                                "The pattern to search for. Treated as a literal string by default."
                            ),
                        },
                        "file_pattern": {
                            "type": "string",
                            "description": (
                                "Glob pattern to filter which files are searched. Defaults to None"
                                " (search all files)."
                            ),
                        },
                        "regex": {
                            "type": "boolean",
                            "description": (
                                "If True, treat search_pattern as a regular expression."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["search_pattern"],
                },
            },
        }

    def run(self, search_pattern, file_pattern=None, regex=False):
        """
        Search for pattern (literal string or regex) in files and add matching files to context as read-only.
        """
        try:
            # Get list of files to search
            if file_pattern:
                # Use glob pattern to filter files
                all_files = self.coder.get_all_relative_files()
                files_to_search = []
                for file in all_files:
                    if fnmatch.fnmatch(file, file_pattern):
                        files_to_search.append(file)

                if not files_to_search:
                    return (
                        "No files matching '{file_pattern}' to search for pattern"
                        " '{search_pattern}'"
                    )
            else:
                # Search all files if no pattern provided
                files_to_search = self.coder.get_all_relative_files()

            # Search for pattern in files
            matches = {}
            for file in files_to_search:
                abs_path = self.coder.abs_root_path(file)
                try:
                    with open(abs_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        match_count = 0
                        if regex:
                            try:
                                matches_found = re.findall(search_pattern, content)
                                match_count = len(matches_found)
                            except re.error as e:
                                # Handle invalid regex patterns gracefully
                                self.coder.io.tool_error(
                                    f"Invalid regex pattern '{search_pattern}': {e}"
                                )
                                # Skip this file for this search if regex is invalid
                                continue
                        else:
                            # Exact string matching
                            match_count = content.count(search_pattern)

                        if match_count > 0:
                            matches[file] = match_count
                except Exception:
                    # Skip files that can't be read (binary, etc.)
                    pass

            # Limit the number of files added if there are too many matches
            if len(matches) > self.coder.max_files_per_glob:
                self.coder.io.tool_output(
                    f"⚠️ Found '{search_pattern}' in {len(matches)} files, "
                    f"limiting to {self.coder.max_files_per_glob} files with most matches."
                )
                # Sort by number of matches (most matches first)
                sorted_matches = sorted(matches.items(), key=lambda x: x[1], reverse=True)
                matches = dict(sorted_matches[: self.coder.max_files_per_glob])

            # Add matching files to context
            if matches:
                # Sort by number of matches (most matches first) for preview
                sorted_matches = sorted(matches.items(), key=lambda x: x[1], reverse=True)
                match_list_preview = [f"{file} ({count} matches)" for file, count in sorted_matches[:5]]

                if not self.coder.io.confirm_ask(
                    f"Allow adding {len(matches)} files containing '{search_pattern}' to chat as read-only?",
                    subject=", ".join(match_list_preview) + (f" and {len(matches) - 5} more" if len(sorted_matches) > 5 else ""),
                ):
                    self.coder.io.tool_output(f"Skipped adding files matching '{search_pattern}'.")
                    return "Action skipped by user."

                for file in matches:
                    self.coder._add_file_to_context(file)

                # Return a user-friendly result
                if len(sorted_matches) > 5:
                    self.coder.io.tool_output(
                        f"🔍 Found '{search_pattern}' in {len(matches)} files:"
                        f" {', '.join(match_list_preview)} and {len(matches) - 5} more"
                    )
                    return (
                        f"Found in {len(matches)} files: {', '.join(match_list_preview)} and"
                        f" {len(matches) - 5} more"
                    )
                else:
                    self.coder.io.tool_output(
                        f"🔍 Found '{search_pattern}' in: {', '.join(match_list_preview)}"
                    )
                    return f"Found in {len(matches)} files: {', '.join(match_list_preview)}"
            else:
                self.coder.io.tool_output(f"⚠️ Pattern '{search_pattern}' not found in any files")
                return "Pattern not found in any files"
        except Exception as e:
            self.coder.io.tool_error(f"Error in ViewFilesMatching: {str(e)}")
            return f"Error: {str(e)}"


def execute_view_files_matching(coder, search_pattern, file_pattern=None, regex=False):
    return ViewFilesMatching(coder).run(
        search_pattern=search_pattern, file_pattern=file_pattern, regex=regex
    )


view_files_matching_schema = ViewFilesMatching.get_tool_definition()
