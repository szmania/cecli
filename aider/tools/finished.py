from .base_tool import BaseAiderTool


class FinishedTool(BaseAiderTool):
    """
    A tool to declare that we are done with every single sub goal and no further work is needed.
    """

    schema = {
        "type": "function",
        "function": {
            "name": "Finished",
            "description": (
                "Declare that we are done with every single sub goal and no further work is needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }

    # Normalized tool name for lookup
    NORM_NAME = "finished"

    def run(self, **kwargs):
        """
        Mark that the current generation task needs no further effort.

        This gives the LLM explicit control over when it can stop looping
        """

        if self.coder:
            self.coder.agent_finished = True
            # self.coder.io.tool_output("Task Finished!")
            return "Task Finished!"

        # self.coder.io.tool_Error("Error: Could not mark agent task as finished")
        return "Error: Could not mark agent task as finished"
