from pathlib import Path
from typing import List

from aider.commands.utils.base_command import BaseCommand
from aider.commands.utils.helpers import format_command_result


class SaveCommand(BaseCommand):
    NORM_NAME = "save"
    DESCRIPTION = "Save commands to a file that can reconstruct the current chat session's files"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the save command with given parameters."""
        if not args.strip():
            io.tool_error("Please provide a filename to save the commands to.")
            return format_command_result(io, "save", "No filename provided")

        try:
            with open(args.strip(), "w", encoding=io.encoding) as f:
                f.write("/drop\n")
                # Write commands to add editable files
                for fname in sorted(coder.abs_fnames):
                    rel_fname = coder.get_rel_fname(fname)
                    f.write(f"/add       {rel_fname}\n")

                # Write commands to add read-only files
                for fname in sorted(coder.abs_read_only_fnames):
                    # Use absolute path for files outside repo root, relative path for files inside
                    if Path(fname).is_relative_to(coder.root):
                        rel_fname = coder.get_rel_fname(fname)
                        f.write(f"/read-only {rel_fname}\n")
                    else:
                        f.write(f"/read-only {fname}\n")
                # Write commands to add read-only stubs files
                for fname in sorted(coder.abs_read_only_stubs_fnames):
                    # Use absolute path for files outside repo root, relative path for files inside
                    if Path(fname).is_relative_to(coder.root):
                        rel_fname = coder.get_rel_fname(fname)
                        f.write(f"/read-only-stub {rel_fname}\n")
                    else:
                        f.write(f"/read-only-stub {fname}\n")

            io.tool_output(f"Saved commands to {args.strip()}")
            return format_command_result(io, "save", f"Saved commands to {args.strip()}")
        except Exception as e:
            io.tool_error(f"Error saving commands to file: {e}")
            return format_command_result(io, "save", f"Error saving commands to file: {e}", e)

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for save command."""
        # For save command, we could return file paths for completion
        # For now, return empty list
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the save command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /save <filename>  # Save commands to reconstruct current chat session\n"
        help_text += "\nExamples:\n"
        help_text += "  /save session.txt  # Save session commands to session.txt\n"
        help_text += "\nThe saved file contains commands that can be used with /load to restore\n"
        help_text += "the current chat session, including all editable and read-only files.\n"
        help_text += "The file starts with /drop to clear existing files, then adds all files.\n"
        return help_text
