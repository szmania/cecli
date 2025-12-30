import math
import os
from aider.tools.utils.base_tool import BaseTool


class Tool(BaseTool):
    """
    A tool to help manage the chat context when it's getting full.
    """

    SCHEMA = {
        "type": "function",
        "function": {
            "name": "ContextManager",
            "description": (
                "Analyzes and manages chat context to free up space when the context limit is"
                " approached. It can list files with token counts, remove files,"
                " or clear chat history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "The action to perform. 'analyze' to get a summary of context with"
                            " token counts. 'remove_largest' to remove the N largest files."
                            " 'remove' to remove specific files. 'clear_history' to clear older"
                            " messages from the chat history."
                        ),
                        "enum": ["analyze", "remove_largest", "remove", "clear_history"],
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "The number of largest files to remove. Used with 'remove_largest'."
                        ),
                        "default": 1,
                    },
                    "file_paths": {
                        "type": "array",
                        "description": "List of file paths to remove. Used with 'remove' action.",
                        "items": {"type": "string"},
                    },
                    "keep_percent": {
                        "type": "integer",
                        "description": "Percentage of recent messages to keep when clearing history (e.g., 25 keeps the most recent 25% of history)",
                        "default": 25,
                    },
                },
                "required": ["action"],
            },
        },
    }

    @classmethod
    async def execute(cls, coder, action, count=1, file_paths=None):
        if action == "analyze":
            summary = coder.get_context_summary()
            if not summary:
                return "Could not generate context summary."
            return summary

        elif action == "remove":
            if not file_paths:
                return "Error: file_paths must be provided for 'remove' action."

            removed_files = []
            errors = []
            for file_path in file_paths:
                try:
                    # drop_rel_fname returns True on success, False on failure
                    if coder.drop_rel_fname(file_path):
                        removed_files.append(file_path)
                    else:
                        # This could happen if the file is not in context
                        errors.append(f"Could not remove '{file_path}' (file not in context?).")
                except Exception as e:
                    errors.append(f"Error removing '{file_path}': {e}")

            result = []
            if removed_files:
                result.append(f"Successfully removed files: {', '.join(removed_files)}")
            if errors:
                result.append(f"Errors: {', '.join(errors)}")

            return "\n".join(result)

        elif action == "remove_largest":
            all_files = list(coder.abs_fnames) + list(coder.abs_read_only_fnames)
            if not all_files:
                return "No files in context to remove."

            file_tokens = []
            for fname in all_files:
                content = coder.io.read_text(fname)
                if content is not None:
                    tokens = coder.main_model.token_count(content)
                    file_tokens.append((fname, tokens))

            file_tokens.sort(key=lambda x: x[1], reverse=True)

            files_to_remove = file_tokens[:count]

            removed_files_info = []
            errors = []
            for fname, tokens in files_to_remove:
                rel_fname = coder.get_rel_fname(fname)
                try:
                    if coder.drop_rel_fname(rel_fname):
                        removed_files_info.append(f"{rel_fname} ({tokens:,} tokens)")
                    else:
                        errors.append(f"Could not remove '{rel_fname}'.")
                except Exception as e:
                    errors.append(f"Error removing '{rel_fname}': {e}")

            result = []
            if removed_files_info:
                result.append(f"Successfully removed largest files: {', '.join(removed_files_info)}")
            if errors:
                result.append(f"Errors: {', '.join(errors)}")

            return "\n".join(result)

        elif action == "clear_history":
            keep_percent = kwargs.get("keep_percent", 25)
            total_messages = len(coder.done_messages)
            
            if total_messages == 0:
                return "No messages in chat history to clear."
                
            # Calculate number of messages to keep (at least 1)
            messages_to_keep = max(1, math.ceil(total_messages * keep_percent / 100))
            
            # Keep only the most recent messages
            kept_messages = coder.done_messages[-messages_to_keep:]
            coder.done_messages[:] = kept_messages
            
            cleared_count = total_messages - messages_to_keep
            return f"Cleared {cleared_count} messages from chat history. Kept the most recent {keep_percent}% ({messages_to_keep} out of {total_messages}) messages."

        else:
            return "Error: Invalid action specified."


def process_response(coder, response):
    """
    Process the response from the tool.
    """
    return Tool.execute(coder, **response)
