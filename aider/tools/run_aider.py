# Import necessary functions
import subprocess
import sys
import os
import shlex
from aider.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    NORM_NAME = "runaider"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "RunAider",
            "description": (
                "Execute an aider command in a separate, isolated subprocess."
                " This is a powerful command and should be used with caution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "string",
                        "description": (
                            "The arguments to pass to the aider command. For example:"
                            " '--version' or 'my_file.py --message \"fix this bug\"'."
                        ),
                    },
                },
                "required": ["args"],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, args):
        """
        Execute an aider command in a separate, isolated subprocess.
        """
        try:
            # Using shlex.split() is safer for parsing command line arguments
            command_list = [sys.executable, "-m", "aider"] + shlex.split(args)
            command_string = " ".join(command_list)

            confirmed = (
                True
                if coder.skip_cli_confirmations
                else await coder.io.confirm_ask(
                    "Allow execution of this aider command in a subprocess?",
                    subject=command_string,
                    explicit_yes_required=True,
                    allow_never=True,
                    group_response="RunAider Tool",
                )
            )

            if not confirmed:
                coder.io.tool_output(f"Skipped execution of aider command: {command_string}")
                return "Aider command execution skipped by user."

            coder.io.tool_output(f"⚙️ Executing aider command in a subprocess: {command_string}")

            # Use a copy of the environment and ensure PATH is present
            env = os.environ.copy()

            result = subprocess.run(
                command_list,
                cwd=coder.root,
                env=env,
                capture_output=True,
                text=True,
                check=False,  # Do not raise exception on non-zero exit
            )

            output = ""
            if result.stdout:
                output += f"--- stdout ---\n{result.stdout}\n"
            if result.stderr:
                output += f"--- stderr ---\n{result.stderr}\n"

            # Truncate long output
            output_limit = coder.large_file_token_threshold
            if len(output) > output_limit:
                output = (
                    output[:output_limit]
                    + f"\n... (output truncated at {output_limit} characters, based on"
                    " large_file_token_threshold)"
                )

            if result.returncode == 0:
                return (
                    "Aider command executed successfully (exit code 0)."
                    f" Output:\n{output}"
                )
            else:
                return (
                    f"Aider command failed with exit code {result.returncode}."
                    f" Output:\n{output}"
                )

        except Exception as e:
            coder.io.tool_error(f"Error executing aider command: {str(e)}")
            return f"Error executing aider command: {str(e)}"
