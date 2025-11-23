import os
from pathlib import Path
import litellm
import importlib.resources


def get_tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "CreateTool",
            "description": "Create a new custom tool by providing a description and a valid Python filename. The new tool will be automatically loaded and available for use.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "A natural language description of the tool to be created. This will be used to generate the tool's Python code.",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "The desired filename for the new tool (e.g., 'my_new_tool.py'). Must end with .py and not contain path separators.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["local", "global"],
                        "default": "local",
                        "description": "The scope for the new tool. Can be 'local' (default) for the current project or 'global' for all projects.",
                    },
                },
                "required": ["description", "file_name"],
            },
        },
    }

def _execute(coder, description: str, file_name: str, scope: str = "local"):
    from aider.utils import strip_fenced_code
    """
    Creates a new custom tool based on the provided description and filename.
    The new tool is then automatically loaded into the current session.
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
        tools_dir = os.path.join(Path.home(), ".aider", "tools")
    else:  # local
        if not coder.repo:
            return "Error: Cannot create local tool without a git repository."
        tools_dir = os.path.join(coder.repo.root, ".aider", "tools")

    # 3. Load Prompt
    try:
        tool_creation_prompt_content = (
            importlib.resources.files("aider.coders.prompts")
            .joinpath("create_tool_prompt.md")
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
        completion = litellm.completion(
            model=coder.main_model.name,
            messages=messages,
            temperature=0,
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
        file_path = os.path.join(tools_dir, file_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(cleaned_code)

        if hasattr(coder, "tool_manager"):
            coder.tool_manager.load_tool(file_path)

        return f"Successfully created tool '{file_name}' in {scope} scope. It will be available in the next chat session."

    except Exception as e:
        return f"Error during tool creation: {e}"
