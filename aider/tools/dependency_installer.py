import subprocess
import sys
from .base_tool import BaseAiderTool

class DependencyInstaller(BaseAiderTool):
    """
    A tool to install python package dependencies using pip.
    """

    def get_tool_definition(self):
        return {
            "type": "function",
            "function": {
                "name": "InstallDependencies",
                "description": (
                    "Installs Python package dependencies using pip. Use this tool when another"
                    " tool fails due to missing packages."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "packages": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "A list of packages to install, e.g., ['requests', 'numpy'].",
                        },
                    },
                    "required": ["packages"],
                },
            },
        }

    def run(self, packages):
        """
        Installs the given packages using pip.

        :param packages: A list of packages to install.
        :return: The output of the pip install command.
        """
        if not packages:
            return "No packages specified for installation."

        command = [sys.executable, "-m", "pip", "install"] + packages

        try:
            self.coder.io.tool_output(f"Running command: {' '.join(command)}")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
            )
            output = "Successfully installed packages:\n"
            if result.stdout:
                output += "--- stdout ---\n"
                output += result.stdout
            if result.stderr:
                output += "--- stderr ---\n"
                output += result.stderr
            return output
        except FileNotFoundError:
            return "Error: 'pip' command not found. Make sure pip is installed and in your PATH."
        except subprocess.CalledProcessError as e:
            error_message = f"Error installing packages: {' '.join(packages)}\n"
            if e.stdout:
                error_message += "--- stdout ---\n"
                error_message += e.stdout
            if e.stderr:
                error_message += "--- stderr ---\n"
                error_message += e.stderr
            return error_message
        except Exception as e:
            return f"An unexpected error occurred: {str(e)}"
