import os

from .base_tool import BaseAiderTool


class ViewFilesWithSymbol(BaseAiderTool):
    """
    Find files containing a symbol using RepoMap and add them to context.
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ViewFilesWithSymbol",
                "description": (
                    "Find files containing a symbol using RepoMap and add them to context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "The symbol to search for.",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        }

    def run(self, symbol):
        """
        Find files containing a symbol using RepoMap and add them to context.
        Checks files already in context first.
        """
        if not self.coder.repo_map:
            self.coder.io.tool_output(
                "⚠️ Repo map not available, cannot use ViewFilesWithSymbol tool."
            )
            return "Repo map not available"

        if not symbol:
            return "Error: Missing 'symbol' parameter for ViewFilesWithSymbol"

        # --- Start Modification ---
        # 1. Check files already in context
        files_in_context = list(self.coder.abs_fnames) + list(self.coder.abs_read_only_fnames)
        found_in_context = []
        for abs_fname in files_in_context:
            rel_fname = self.coder.get_rel_fname(abs_fname)
            try:
                # Use get_tags for consistency with RepoMap usage elsewhere for now.
                tags = self.coder.repo_map.get_tags(abs_fname, rel_fname)
                for tag in tags:
                    if tag.name == symbol:
                        found_in_context.append(rel_fname)
                        break  # Found in this file, move to next
            except Exception as e:
                self.coder.io.tool_warning(
                    f"Could not get symbols for {rel_fname} while checking context: {e}"
                )

        if found_in_context:
            # Symbol found in already loaded files. Report this and stop.
            file_list = ", ".join(sorted(list(set(found_in_context))))
            self.coder.io.tool_output(
                f"Symbol '{symbol}' found in already loaded file(s): {file_list}. No external"
                " search performed."
            )
            return (
                f"Symbol '{symbol}' found in already loaded file(s): {file_list}. No external"
                " search performed."
            )
        # --- End Modification ---

        # 2. If not found in context, search the repository using RepoMap
        self.coder.io.tool_output(
            f"🔎 Searching for symbol '{symbol}' in repository (excluding current context)..."
        )
        try:
            found_files = set()
            current_context_files = self.coder.abs_fnames | self.coder.abs_read_only_fnames
            files_to_search = set(self.coder.get_all_abs_files()) - current_context_files

            rel_fname_to_abs = {}
            all_tags = []

            for fname in files_to_search:
                rel_fname = self.coder.get_rel_fname(fname)
                rel_fname_to_abs[rel_fname] = fname
                try:
                    tags = self.coder.repo_map.get_tags(fname, rel_fname)
                    all_tags.extend(tags)
                except Exception as e:
                    self.coder.io.tool_warning(f"Could not get tags for {rel_fname}: {e}")

            # Find matching symbols
            for tag in all_tags:
                if tag.name == symbol:
                    # Use absolute path directly if available, otherwise resolve from relative path
                    abs_fname = rel_fname_to_abs.get(tag.rel_fname) or self.coder.abs_root_path(
                        tag.fname
                    )
                    if (
                        abs_fname in files_to_search
                    ):  # Ensure we only add files we intended to search
                        found_files.add(abs_fname)

            # Limit the number of files added
            if len(found_files) > self.coder.max_files_per_glob:
                self.coder.io.tool_output(
                    f"⚠️ Found symbol '{symbol}' in {len(found_files)} files, "
                    f"limiting to {self.coder.max_files_per_glob} most relevant files."
                )
                # Sort by modification time (most recent first) - approximate relevance
                sorted_found_files = sorted(
                    list(found_files), key=lambda f: os.path.getmtime(f), reverse=True
                )
                found_files = set(sorted_found_files[: self.coder.max_files_per_glob])

            # Add files to context (as read-only)
            added_count = 0
            added_files_rel = []

            if found_files:
                file_list_preview = ", ".join(sorted([self.coder.get_rel_fname(f) for f in list(found_files)[:5]]))
                if len(found_files) > 5:
                    file_list_preview += f", and {len(found_files) - 5} more"

                if not self.coder.io.confirm_ask(
                    f"Allow adding {len(found_files)} files containing symbol '{symbol}' to chat as read-only?",
                    subject=file_list_preview,
                ):
                    self.coder.io.tool_output(f"Skipped adding files containing symbol '{symbol}'.")
                    return "Action skipped by user."

                for abs_file_path in found_files:
                    rel_path = self.coder.get_rel_fname(abs_file_path)
                    # Double check it's not already added somehow
                    if (
                        abs_file_path not in self.coder.abs_fnames
                        and abs_file_path not in self.coder.abs_read_only_fnames
                    ):
                        # Use explicit=True for clear output, even though it's an external search result
                        add_result = self.coder._add_file_to_context(rel_path, explicit=True)
                        if (
                            "Added" in add_result or "Viewed" in add_result
                        ):  # Count successful adds/views
                            added_count += 1
                            added_files_rel.append(rel_path)

            if added_count > 0:
                if added_count > 5:
                    brief = ", ".join(added_files_rel[:5]) + f", and {added_count - 5} more"
                    self.coder.io.tool_output(
                        f"🔎 Found '{symbol}' and added {added_count} files: {brief}"
                    )
                else:
                    self.coder.io.tool_output(
                        f"🔎 Found '{symbol}' and added files: {', '.join(added_files_rel)}"
                    )
                return f"Found symbol '{symbol}' and added {added_count} files as read-only."
            else:
                self.coder.io.tool_output(
                    f"⚠️ Symbol '{symbol}' not found in searchable files (outside current context)."
                )
                return (
                    f"Symbol '{symbol}' not found in searchable files (outside current context)."
                )

        except Exception as e:
            self.coder.io.tool_error(f"Error in ViewFilesWithSymbol: {str(e)}")
            return f"Error: {str(e)}"


def _execute_view_files_with_symbol(coder, symbol):
    return ViewFilesWithSymbol(coder).run(symbol=symbol)


view_files_with_symbol_schema = ViewFilesWithSymbol.get_tool_definition()
