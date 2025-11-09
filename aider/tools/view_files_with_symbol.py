from .base_tool import BaseAiderTool


class ViewFilesWithSymbol(BaseAiderTool):
    """
    View files that contain a specific symbol (e.g., class, function).
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "ViewFilesWithSymbol",
                "description": "View files that contain a specific symbol (e.g., class, function).",
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
                f"Symbol '{symbol}' found in already loaded file(s): {file_list}"
            )
            return f"Symbol '{symbol}' found in already loaded file(s): {file_list}"

        # 2. If not found in context, search the repository using RepoMap
        self.coder.io.tool_output(f"🔎 Searching for symbol '{symbol}' in repository...")
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
                    if abs_fname in files_to_search:  # Ensure we only add files we intended to search
                        found_files.add(self.coder.get_rel_fname(abs_fname))

            # Add files to context
            if found_files:
                found_files_list = sorted(list(found_files))

                # Limit the number of files added if there are too many matches
                if len(found_files_list) > self.coder.max_files_per_glob:
                    self.coder.io.tool_output(
                        f"⚠️ Found symbol '{symbol}' in {len(found_files_list)} files, "
                        f"limiting to {self.coder.max_files_per_glob} most relevant files."
                    )
                    found_files_list = found_files_list[: self.coder.max_files_per_glob]

                file_list_preview = ", ".join(found_files_list[:5])
                if len(found_files_list) > 5:
                    file_list_preview += f", and {len(found_files_list) - 5} more"

                if not self.coder.io.confirm_ask(
                    f"Allow adding {len(found_files_list)} files with symbol '{symbol}' to chat as"
                    " read-only?",
                    subject=file_list_preview,
                ):
                    self.coder.io.tool_output(f"Skipped adding files with symbol '{symbol}'.")
                    return "Action skipped by user."

                for file in found_files_list:
                    self.coder._add_file_to_context(file)

                # Return a user-friendly result
                if len(found_files_list) > 10:
                    brief = (
                        ", ".join(found_files_list[:5]) + f", and {len(found_files_list) - 5} more"
                    )
                    self.coder.io.tool_output(
                        f"📂 Added {len(found_files_list)} files with symbol '{symbol}': {brief}"
                    )
                else:
                    self.coder.io.tool_output(
                        f"📂 Added files with symbol '{symbol}': {', '.join(found_files_list)}"
                    )
                return (
                    f"Added {len(found_files_list)} files:"
                    f" {', '.join(found_files_list[:5])}"
                    f"{' and more' if len(found_files_list) > 5 else ''}"
                )
            else:
                self.coder.io.tool_output(f"⚠️ Symbol '{symbol}' not found in searchable files")
                return f"Symbol '{symbol}' not found in searchable files"

        except Exception as e:
            self.coder.io.tool_error(f"Error in ViewFilesWithSymbol: {str(e)}")
            return f"Error: {str(e)}"


def execute_view_files_with_symbol(coder, symbol):
    return ViewFilesWithSymbol(coder).run(symbol=symbol)


view_files_with_symbol_schema = ViewFilesWithSymbol.get_tool_definition()
