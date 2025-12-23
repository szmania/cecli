import os
from pathlib import Path
import importlib.resources
import litellm
import asyncio
import re
import sys

from aider.run_cmd import run_cmd
from aider.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    NORM_NAME = "createtool"
    SCHEMA = {
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

    @classmethod
    async def execute(cls, coder, description: str, file_name: str, scope: str = "local"):
        from aider.utils import strip_fenced_code

        coder.io.tool_output("Executing CreateTool from aider/tools/create_tool.py (class-based)")

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

        # Check if file already exists before generating code
        file_path = os.path.join(tools_dir, file_name)
        if os.path.exists(file_path):
            return f"Error: Tool file '{file_name}' already exists in {scope} scope."

        # 3. Define Prompt
        tool_creation_prompt_content = """You are an expert software engineer. Your task is to generate the complete Python code for a custom tool for the 'aider' coding assistant.

The user will provide a description of the tool. You must generate a single Python script that implements this tool.

The generated tool code MUST follow this structure:

1.  It must be a class named `Tool` that inherits from `aider.tools.utils.base_tool.BaseTool`.
2.  The `Tool` class must define two class attributes: `NORM_NAME` and `SCHEMA`.
3.  The `Tool` class must implement an `async` class method `execute(cls, coder, **params)`.

Here is a template:

```python
from aider.tools.utils.base_tool import BaseTool
# Add any other necessary imports here

class Tool(BaseTool):
    \"\"\"
    A brief description of the tool.
    \"\"\"

    # NORM_NAME should be the lowercase version of the function name in the SCHEMA
    NORM_NAME = "my_tool_name"
    SCHEMA = {
        "type": "function",
        "function": {
            "name": "MyToolName",
            "description": "A detailed description of what the tool does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "param1": {
                        "type": "string",
                        "description": "Description of parameter 1."
                    },
                    "param2": {
                        "type": "boolean",
                        "description": "Description of parameter 2."
                    }
                },
                "required": ["param1"]
            }
        }
    }

    @classmethod
    async def execute(cls, coder, **params):
        \"\"\"
        The implementation of the tool.

        Args:
            coder: The Coder instance, providing access to I/O and other context.
            **params: A dictionary of parameters matching the SCHEMA.

        Returns:
            str: A string containing the result of the tool's execution.
        \"\"\"
        # Your tool logic goes here.
        # Access parameters via params['param_name']
        # Use coder.io.tool_output("message") for printing status messages.
        # Use coder.io.tool_error("message") for errors.
        return "Tool logic not implemented yet."
```

**IMPORTANT INSTRUCTIONS:**
-   Infer the `name`, `description`, `parameters` (including properties and required fields) for the `SCHEMA` from the user's tool description.
-   The `NORM_NAME` must be the lowercase version of the `name` in the `SCHEMA`.
-   The `execute` method must be an `async` class method.
-   The code should be self-contained in a single script. Do not assume other files are present unless they are standard Python libraries.
-   Only output the raw Python code inside a single ` ```python ... ``` ` block. Do not include any other text, explanation, or preamble.
"""

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
            coder.io.tool_output(f"Creating new tool '{file_name}' in {scope} scope...")
            coder.io.tool_output("Generating new tool code with an LLM call...")
            
            # Use the main model's configuration parameters to ensure proper API setup
            kwargs = coder.main_model.extra_params.copy()
            kwargs.update(
                dict(
                    model=coder.main_model.name,
                    messages=messages,
                    stream=True,  # Changed to streaming
                    temperature=0,
                )
            )
            
            # 6. Call LLM with streaming and collect response
            completion = await litellm.acompletion(**kwargs)
            
            # Collect streaming chunks
            full_response = ""
            async for chunk in completion:
                if chunk.choices and chunk.choices[0].delta.content:
                    full_response += chunk.choices[0].delta.content
            
            # Create a mock response object to match non-streaming format
            class MockChoice:
                def __init__(self, content):
                    self.message = type('obj', (object,), {'content': content})()
            
            class MockCompletion:
                def __init__(self, content):
                    self.choices = [MockChoice(content)]
            
            completion = MockCompletion(full_response)

            # 7. Parse and Clean Response
            generated_code = completion.choices[0].message.content
            if not generated_code:
                return "Error: LLM did not generate any code for the tool."

            coder.io.tool_output(f"LLM raw response:\n---\n{generated_code}\n---")

            cleaned_code = strip_fenced_code(generated_code)
            if not cleaned_code.strip():
                # Fallback: if no fenced code, maybe the whole response is code
                if "def get_tool_definition():" in generated_code and "async def _execute(" in generated_code:
                    cleaned_code = generated_code
                else:
                    error_msg = "Error: Generated tool code is empty after cleaning."
                    error_msg += f"\n\nLLM Raw Response:\n{generated_code}"
                    return error_msg

            # 7. Save and Load
            os.makedirs(tools_dir, exist_ok=True)
            # file_path is already defined above
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cleaned_code)

            # 8. Detect and handle dependencies
            dependencies = []
            install_comment_pattern = re.compile(r"#.*pip install\s+(.*)", re.IGNORECASE)
            for line in cleaned_code.splitlines():
                match = install_comment_pattern.search(line)
                if match:
                    packages = match.group(1).strip().split()
                    dependencies.extend(p for p in packages if p)

            final_message_suffix = " It is now loaded and available for use."

            if dependencies:
                requirements_path = os.path.join(tools_dir, "requirements.txt")
                existing_reqs = set()
                if os.path.exists(requirements_path):
                    with open(requirements_path, "r", encoding="utf-8") as f:
                        existing_reqs = set(line.strip() for line in f if line.strip())

                new_deps = set(dependencies) - existing_reqs

                if new_deps:
                    sorted_new_deps = sorted(list(new_deps))
                    with open(requirements_path, "a", encoding="utf-8") as f:
                        for dep in sorted_new_deps:
                            f.write(f"\n{dep}")

                    deps_str = ", ".join(sorted_new_deps)
                    coder.io.tool_output(
                        f"Detected and added new dependencies to {requirements_path}: {deps_str}"
                    )

                    if await coder.io.confirm_ask("Do you want to install these dependencies now?"):
                        install_command = (
                            f'"{sys.executable}" -m pip install -r "{requirements_path}"'
                        )
                        coder.io.tool_output(f"Running: {install_command}")

                        exit_code, output = await asyncio.to_thread(run_cmd, install_command)

                        if exit_code == 0:
                            coder.io.tool_output("Dependencies installed successfully.")
                            final_message_suffix = (
                                "\nDependencies installed. The tool is now loaded and available for"
                                " use."
                            )
                        else:
                            coder.io.tool_error("Dependency installation failed.")
                            coder.io.tool_error(output)
                            final_message_suffix = (
                                "\nSome dependencies failed to install. The tool may not work"
                                " correctly. You may need to install them manually from"
                                f" '{requirements_path}'."
                            )
                    else:
                        final_message_suffix = (
                            "\nDependencies detected but not installed. The tool may fail to load."
                            f" You can install them later from '{requirements_path}'."
                        )
                else:
                    coder.io.tool_output("Detected dependencies are already in requirements.txt.")

            if hasattr(coder, "tool_manager"):
                await coder.tool_manager.load_tool_async(file_path)

            success_msg = f"Successfully created tool '{file_name}' in {scope} scope."
            return success_msg + final_message_suffix

        except litellm.exceptions.APIError as e:
            return f"Error: Model API call failed: {e}"
        except Exception as e:
            return f"An unexpected error occurred during tool creation: {e}"
