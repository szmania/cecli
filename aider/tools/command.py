from .base_tool import BaseAiderTool
from aider.run_cmd import run_cmd_subprocess


class Command(BaseAiderTool):
    """
    Execute a non-interactive shell command after user confirmation.
    """

    def get_tool_definition(self):
        return {
            "type": "function",
            "function": {
                "name": "Command",
                "description": "Execute a non-interactive shell command after user confirmation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_string": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        },
                    },
                    "required": ["command_string"],
                },
            },
        }

    def run(self, command_string):
        """
        Execute a non-interactive shell command after user confirmation.
        """
        try:
            # Ask for confirmation before executing.
            # allow_never=True enables the 'Always' option.
            # confirm_ask handles remembering the 'Always' choice based on the subject.
            confirmed = self.coder.io.confirm_ask(
                "Allow execution of this command?",
                subject=command_string,
                explicit_yes_required=True,  # Require explicit 'yes' or 'always'
                allow_never=True,  # Enable the 'Always' option
            )

            if not confirmed:
                # This happens if the user explicitly says 'no' this time.
                # If 'Always' was chosen previously, confirm_ask returns True directly.
                self.coder.io.tool_output(f"Skipped execution of shell command: {command_string}")
                return "Shell command execution skipped by user."

            # Proceed with execution if confirmed is True
            self.coder.io.tool_output(
                f"⚙️ Executing non-interactive shell command: {command_string}"
            )

            # Use run_cmd_subprocess for non-interactive execution
            exit_status, combined_output = run_cmd_subprocess(
                command_string,
                verbose=self.coder.verbose,
                cwd=self.coder.root,  # Execute in the project root
            )

            # Format the output for the result message, include more content
            output_content = combined_output or ""
            # Use the existing token threshold constant as the character limit for truncation
            output_limit = self.coder.large_file_token_threshold
            if len(output_content) > output_limit:
                # Truncate and add a clear message using the constant value
                output_content = (
                    output_content[:output_limit]
                    + "\n... (output truncated at"
                    f" {output_limit} characters, based on large_file_token_threshold)"
                )

            if exit_status == 0:
                return (
                    "Shell command executed successfully (exit code 0)."
                    f" Output:\n{output_content}"
                )
            else:
                return (
                    f"Shell command failed with exit code {exit_status}. Output:\n{output_content}"
                )

        except Exception as e:
            self.coder.io.tool_error(
                f"Error executing non-interactive shell command '{command_string}': {str(e)}"
            )
            # Optionally include traceback for debugging if verbose
            # if self.coder.verbose:
            #     self.coder.io.tool_error(traceback.format_exc())
            return f"Error executing command: {str(e)}"


def _execute_command(coder, command_string):
    return Command(coder).run(command_string=command_string)
