# Import necessary functions
from cecli.helpers.background_commands import BackgroundCommandManager
from cecli.run_cmd import run_cmd_subprocess
from cecli.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    NORM_NAME = "command"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "Command",
            "description": "Execute a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_string": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Run command in background (non-blocking).",
                        "default": False,
                    },
                    "stop_background": {
                        "type": "string",
                        "description": "Command string to stop if running in background.",
                    },
                },
                "required": ["command_string"],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, command_string, background=False, stop_background=None, **kwargs):
        """
        Execute a shell command, optionally in background.
        """
        # Handle stopping background commands
        if stop_background:
            return await cls._stop_background_command(coder, stop_background)

        # Check for implicit background (trailing & on Linux)
        if not background and command_string.strip().endswith("&"):
            background = True
            command_string = command_string.strip()[:-1].strip()

        # Get user confirmation
        confirmed = await cls._get_confirmation(coder, command_string, background)
        if not confirmed:
            return "Command execution skipped by user."

        if background:
            return await cls._execute_background(coder, command_string)
        else:
            return await cls._execute_foreground(coder, command_string)

    @classmethod
    async def _get_confirmation(cls, coder, command_string, background):
        """Get user confirmation for command execution."""
        if coder.skip_cli_confirmations:
            return True

        command_string = coder.format_command_with_prefix(command_string)

        if background:
            prompt = "Allow execution of this background command?"
        else:
            prompt = "Allow execution of this command?"

        return await coder.io.confirm_ask(
            prompt,
            subject=command_string,
            explicit_yes_required=True,
            allow_never=True,
            group_response="Command Tool",
        )

    @classmethod
    async def _execute_background(cls, coder, command_string):
        """
        Execute command in background.
        """
        coder.io.tool_output(f"⚙️ Starting background command: {command_string}")

        # Use static manager to start background command
        command_key = BackgroundCommandManager.start_background_command(
            command_string, verbose=coder.verbose, cwd=coder.root, max_buffer_size=4096
        )

        return (
            f"Background command started: {command_string}\n"
            f"Command key: {command_key}\n"
            "Output will be injected into chat stream."
        )

    @classmethod
    async def _execute_foreground(cls, coder, command_string):
        """
        Execute command in foreground (blocking).
        """
        should_print = True
        tui = None
        if coder.tui and coder.tui():
            tui = coder.tui()
            should_print = False

        coder.io.tool_output(f"⚙️ Executing shell command: {command_string}")

        # Use run_cmd_subprocess for non-interactive execution
        exit_status, combined_output = run_cmd_subprocess(
            command_string,
            verbose=coder.verbose,
            cwd=coder.root,
            should_print=should_print,
        )

        # Format the output for the result message
        output_content = combined_output or ""
        output_limit = coder.large_file_token_threshold
        if len(output_content) > output_limit:
            output_content = (
                output_content[:output_limit]
                + f"\n... (output truncated at {output_limit} characters, based on"
                " large_file_token_threshold)"
            )

        if tui:
            coder.io.tool_output(output_content)

        if exit_status == 0:
            return f"Shell command executed successfully (exit code 0). Output:\n{output_content}"
        else:
            return f"Shell command failed with exit code {exit_status}. Output:\n{output_content}"

    @classmethod
    async def _stop_background_command(cls, coder, command_key):
        """
        Stop a running background command.
        """
        success, output, exit_code = BackgroundCommandManager.stop_background_command(command_key)

        if success:
            return (
                f"Background command stopped: {command_key}\n"
                f"Exit code: {exit_code}\n"
                f"Final output:\n{output}"
            )
        else:
            return output  # Error message from manager

    @classmethod
    async def _handle_errors(cls, coder, command_string, e):
        """Handle errors during command execution."""
        coder.io.tool_error(f"Error executing shell command '{command_string}': {str(e)}")
        return f"Error executing command: {str(e)}"
