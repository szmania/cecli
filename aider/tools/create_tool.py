import os
from pathlib import Path

import importlib.resources  # Added import
import litellm
from aider.utils import strip_fenced_code  # Added import

from aider.tools.base_tool import BaseAiderTool


class CreateTool(BaseAiderTool):
    """
    A tool that allows the LLM to create new custom tools.
    """

    name = "CreateTool"
    description = (
        "Create a new custom tool by providing a description and filename. The new"
        " tool will be automatically loaded and available for use."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "A natural language description of the tool to be created. This will be"
                    " used to generate the tool's Python code."
                ),
            },
            "file_name": {
                "type": "string",
                "description": (
                    "The desired filename for the new tool (e.g., 'my_new_tool.py'). Must"
                    " end with .py and not contain path separators."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["local", "global"],
                "default": "local",
                "description": (
                    "The scope of the tool. 'local' for the current project, 'global'"
                    " for all projects. Defaults to 'local'."
                ),
            },
        },
        "required": ["description", "file_name"],
    }

    def run(self, description: str, file_name: str, scope: str = "local"):
        """
        Creates a new custom tool based on the provided description and filename.
        The new tool is then automatically loaded into the current session.

        :param description: A natural language description of the tool to be created.
        :param file_name: The desired filename for the new tool (e.g., 'my_new_tool.py').
                          Must end with .py and not contain path separators.
        :param scope: The scope of the tool, either "local" (for the current project) or "global" (for all projects).
        :return: A message indicating success or failure.
        """
        # 1. Validate Inputs
        if not file_name.lower().endswith(".py"):
            return f"Error: file_name '{file_name}' must end with '.py'."
        if "/" in file_name or "\\" in file_name:
            return f"Error: file_name '{file_name}' must not contain path separators."
        if scope not in ["local", "global"]:
            return f"Error: scope '{scope}' must be either 'local' or 'global'."

        # 2. Determine Target Directory
        if scope == "global":
            tools_dir = self.coder._get_global_tools_dir()
        else:  # local
            tools_dir = self.coder._get_local_tools_dir()

        # 3. Load Prompt
        try:
            tool_creation_prompt_content = (
                importlib.resources.files("aider.coders.prompts")
                .joinpath("navigator_tool_create.md")
                .read_text(encoding="utf-8")
            )
        except Exception as e:
            return f"Error reading tool creation prompt file: {e}"

        # 4. Construct LLM Request
        messages = [
            {"role": "system", "content": tool_creation_prompt_content},
            {
                "role": "user",
                "content": (
                    f"Generate the complete Python code for a tool based on the following description."
                    f" The tool should be saved as '{file_name}'.\n\nTool Description:\n{description}"
                ),
            },
        ]

        # 5. Call LLM
        try:
            # Use the coder's main_model for generating the tool code
            # Set temperature to 0 for deterministic output
            completion = litellm.completion(
                model=self.coder.main_model.name,
                messages=messages,
                temperature=0,
                # Do not include tools or tool_choice, as the LLM should just output code
            )

            # 6. Parse and Clean Response
            generated_code = completion.choices[0].message.content
            if not generated_code:
                return "Error: LLM did not generate any code for the tool."

            cleaned_code = strip_fenced_code(generated_code)
            if not cleaned_code.strip():
                return "Error: Generated tool code is empty after cleaning."

            # 7. Save and Load
            os.makedirs(tools_dir, exist_ok=True)
            abs_new_tool_path = Path(tools_dir) / file_name

            self.coder.io.tool_output(f"Saving new tool to: {abs_new_tool_path}")
            try:
                with open(abs_new_tool_path, "w", encoding="utf-8") as f:
                    f.write(cleaned_code)
            except IOError as e:
                return f"Error saving tool to '{abs_new_tool_path}': {e}"

            # Load the newly created tool into the coder's session
            self.coder.tool_add_from_path(str(abs_new_tool_path))

            # 8. Return Result
            return f"Tool '{file_name}' created, loaded, and is now available for use."

        except litellm.exceptions.LiteLLMError as e:
            return f"Error calling LLM to create tool: {e}"
        except Exception as e:
            self.coder.io.tool_error(f"An unexpected error occurred during tool creation: {e}")
            return f"An unexpected error occurred during tool creation: {e}"
