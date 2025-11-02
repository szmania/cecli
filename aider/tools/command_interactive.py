from .base_tool import BaseAiderTool
from aider.run_cmd import run_cmd


class CommandInteractive(BaseAiderTool):
    """
    Execute an interactive shell command using run_cmd (which uses pexpect/PTY).
    """

    @staticmethod
    def get_tool_definition():
        return {
            "type": "function",
            "function": {
                "name": "CommandInteractive",
                "description": "Execute an interactive shell command.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_string": {
                            "type": "string",
                            "description": "The interactive shell command to execute.",
                        },
                    },
                    "required": ["command_string"],
                },
            },
        }

    def run(self, command_string):
        """
        Execute an interactive shell command using run_cmd (which uses pexpect/PTY).
        """
        try:
            self.coder.io.tool_output(f"⚙️ Starting interactive shell command: {command_string}")
            self.coder.io.tool_output(">>> You may need to interact with the command below <<<")

            # Use run_cmd which handles PTY logic
            exit_status, combined_output = run_cmd(
                command_string,
                verbose=self.coder.verbose,  # Pass verbose flag
                error_print=self.coder.io.tool_error,  # Use io for error printing
                cwd=self.coder.root,  # Execute in the project root
            )

            self.coder.io.tool_output(">>> Interactive command finished <<<")

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
                    "Interactive command finished successfully (exit code 0)."
                    f" Output:\n{output_content}"
                )
            else:
                return (
                    f"Interactive command finished with exit code {exit_status}."
                    f" Output:\n{output_content}"
                )

        except Exception as e:
            self.coder.io.tool_error(
                f"Error executing interactive shell command '{command_string}': {str(e)}"
            )
            # Optionally include traceback for debugging if verbose
            # if self.coder.verbose:
            #     self.coder.io.tool_error(traceback.format_exc())
            return f"Error executing interactive command: {str(e)}"


def _execute_command_interactive(coder, command_string):
    return CommandInteractive(coder).run(command_string=command_string)


command_interactive_schema = CommandInteractive.get_tool_definition()
