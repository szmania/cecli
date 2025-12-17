import io
import contextlib
from aider.tools.utils.base_tool import BaseTool

class Tool(BaseTool):
    """
    A test runner tool to verify the functionality of other tool-related commands.
    """

    NORM_NAME = "test_runner"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "TestRunner",
            "description": "Runs internal tests for aider's tool management commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_name": {
                        "type": "string",
                        "description": "The name of the test to run.",
                        "enum": ["test_tools_command"]
                    }
                },
                "required": ["test_name"]
            }
        }
    }

    @classmethod
    async def execute(cls, coder, test_name: str):
        """
        The implementation of the test runner tool.
        """
        if not hasattr(cls, test_name):
            return f"Error: Test '{test_name}' not found."

        test_function = getattr(cls, test_name)
        
        # Redirect stdout to capture output from print()
        output_capture = io.StringIO()
        
        # Also redirect coder.io.tool_output
        original_tool_output = coder.io.tool_output
        captured_tool_output = []
        def capture_output(*messages, log_only=False, bold=False):
            captured_tool_output.append(" ".join(map(str, messages)))

        coder.io.tool_output = capture_output

        try:
            with contextlib.redirect_stdout(output_capture):
                await test_function(coder)
            
            # Combine outputs
            stdout_result = output_capture.getvalue()
            tool_output_result = "\n".join(captured_tool_output)

            return f"STDOUT:\n{stdout_result}\n\nTOOL_OUTPUT:\n{tool_output_result}"

        except Exception as e:
            import traceback
            return f"Error running test '{test_name}': {e}\n{traceback.format_exc()}"
        finally:
            # Restore the original function
            coder.io.tool_output = original_tool_output

    @staticmethod
    async def test_tools_command(coder):
        """
        Tests the /tools command.
        This function will now have its output (both print and tool_output)
        captured by the execute method.
        """
        await coder.commands.cmd_tools("")
