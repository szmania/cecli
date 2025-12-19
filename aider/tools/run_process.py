import shlex
import subprocess
import os

from aider.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    NORM_NAME = "runprocess"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "RunProcess",
            "description": "Execute a command in a separate, isolated subprocess.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute, with arguments.",
                    },
                },
                "required": ["command"],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, command):
        """
        Execute a command in a subprocess after user confirmation.
        """
        try:
            # Ask for confirmation before executing.
            confirmed = (
                True
                if coder.skip_cli_confirmations
                else await coder.io.confirm_ask(
                    "Allow execution of this command in a subprocess?",
                    subject=command,
                    explicit_yes_required=True,
                    allow_never=True,
                    group_response="RunProcess Tool",
                )
            )

            if not confirmed:
                coder.io.tool_output(f"Skipped execution of command: {command}")
                return "Command execution skipped by user."

            coder.io.tool_output(f"Executing command in subprocess: {command}")

            # Use shlex.split to safely parse the command string
            args = shlex.split(command)

            # Execute the command as a subprocess
            process = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=coder.root,
                check=False,  # Don't raise exception on non-zero exit
                env=os.environ.copy(),
            )

            exit_status = process.returncode
            stdout = process.stdout or ""
            stderr = process.stderr or ""

            output = ""
            if stdout:
                output += f"--- stdout ---\n{stdout}\n"
            if stderr:
                output += f"--- stderr ---\n{stderr}\n"


            # Truncate output
            output_limit = coder.large_file_token_threshold
            if len(output) > output_limit:
                output = (
                    output[:output_limit]
                    + f"\n... (output truncated at {output_limit} characters)"
                )

            if exit_status == 0:
                return f"Command executed successfully (exit code 0). Output:\n{output}"
            else:
                return f"Command failed with exit code {exit_status}. Output:\n{output}"

        except FileNotFoundError:
            cmd_name = shlex.split(command)[0]
            coder.io.tool_error(f"Error: Command not found: '{cmd_name}'")
            return f"Error: Command not found: '{cmd_name}'"
        except Exception as e:
            coder.io.tool_error(f"Error executing command '{command}': {str(e)}")
            return f"Error executing command: {str(e)}"