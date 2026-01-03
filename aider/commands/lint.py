from typing import List

from aider.commands.utils.base_command import BaseCommand
from aider.commands.utils.helpers import format_command_result


class LintCommand(BaseCommand):
    NORM_NAME = "lint"
    DESCRIPTION = "Lint and fix in-chat files or all dirty files if none in chat"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the lint command with given parameters."""
        fnames = kwargs.get("fnames", None)

        if not coder.repo:
            io.tool_error("No git repository found.")
            return format_command_result(io, "lint", "No git repository found")

        if not fnames:
            fnames = coder.get_inchat_relative_files()

        # If still no files, get all dirty files in the repo
        if not fnames and coder.repo:
            fnames = coder.repo.get_dirty_files()

        if not fnames:
            io.tool_warning("No dirty files to lint.")
            return format_command_result(io, "lint", "No dirty files to lint")

        fnames = [coder.abs_root_path(fname) for fname in fnames]

        lint_coder = None
        for fname in fnames:
            try:
                errors = coder.linter.lint(fname)
            except FileNotFoundError as err:
                io.tool_error(f"Unable to lint {fname}")
                io.tool_output(str(err))
                continue

            if not errors:
                continue

            io.tool_output(errors)
            if not await io.confirm_ask(f"Fix lint errors in {fname}?", default="y"):
                continue

            # Commit everything before we start fixing lint errors
            if coder.repo.is_dirty() and coder.dirty_commits:
                # Use the commit command from registry
                from aider.commands import CommandRegistry

                await CommandRegistry.execute("commit", io, coder, "")

            if not lint_coder:
                lint_coder = await coder.clone(
                    # Clear the chat history, fnames
                    cur_messages=[],
                    done_messages=[],
                    fnames=None,
                )

            lint_coder.add_rel_fname(fname)
            await lint_coder.run_one(errors, preproc=False)
            lint_coder.abs_fnames = set()

        if lint_coder and coder.repo.is_dirty() and coder.auto_commits:
            # Use the commit command from registry
            from aider.commands import CommandRegistry

            await CommandRegistry.execute("commit", io, coder, "")

        return format_command_result(io, "lint", "Linting completed")

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for lint command."""
        # For lint command, we could return file paths for completion
        # For now, return empty list
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the lint command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /lint              # Lint all in-chat files or dirty files\n"
        help_text += "  /lint <files>      # Lint specific files\n"
        help_text += (
            "\nThis command lints files using the configured linter and offers to fix any errors"
            " found.\n"
        )
        help_text += (
            "If no files are specified, it lints all files in the chat or all dirty files in the"
            " repository.\n"
        )
        help_text += "For each file with lint errors, you'll be asked if you want to fix them.\n"
        return help_text
