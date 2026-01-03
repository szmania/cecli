from typing import List

from aider.commands.utils.base_command import BaseCommand
from aider.commands.utils.helpers import format_command_result


class LoadCommand(BaseCommand):
    NORM_NAME = "load"
    DESCRIPTION = "Load and execute commands from a file"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the load command with given parameters."""
        if not args.strip():
            io.tool_error("Please provide a filename containing commands to load.")
            return format_command_result(io, "load", "No filename provided")

        try:
            with open(args.strip(), "r", encoding=io.encoding, errors="replace") as f:
                commands = f.readlines()
        except FileNotFoundError:
            io.tool_error(f"File not found: {args}")
            return format_command_result(io, "load", f"File not found: {args}")
        except Exception as e:
            io.tool_error(f"Error reading file: {e}")
            return format_command_result(io, "load", f"Error reading file: {e}")

        # Get the Commands instance from kwargs if available
        commands_instance = kwargs.get("commands_instance")

        if not commands_instance:
            # Create a minimal Commands instance if not provided
            from aider.commands import Commands

            commands_instance = Commands(io, coder)

        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith("#"):
                continue

            io.tool_output(f"\nExecuting: {cmd}")
            try:
                await commands_instance.run(cmd)
            except Exception as e:
                # Handle SwitchCoder exception specifically
                if type(e).__name__ == "SwitchCoder":
                    io.tool_error(
                        f"Command '{cmd}' is only supported in interactive mode, skipping."
                    )
                else:
                    # Re-raise other exceptions
                    raise

        return format_command_result(
            io, "load", f"Loaded and executed commands from {args.strip()}"
        )

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for load command."""
        return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the load command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /load <filename>  # Load and execute commands from a file\n"
        help_text += "\nExamples:\n"
        help_text += "  /load commands.txt  # Execute commands from commands.txt\n"
        help_text += (
            "\nThe file should contain one command per line. Lines starting with # are ignored.\n"
        )
        help_text += "Commands are executed sequentially as if they were typed interactively.\n"
        return help_text
