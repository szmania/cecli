---
parent: Configuration
nav_order: 35
description: Create and use custom commands to extend cecli's functionality.
---

# Hooks

Hooks allow you to extend `cecli` by defining custom actions that trigger at specific points in the agent's workflow. You can use hooks to automate tasks, integrate with external systems, or add custom validation logic.

## Getting Started

Hooks are configured in your `.cecli.conf.yml` file under the `hooks` section. You can define two types of hooks:
1. **Command Hooks**: Execute shell commands or scripts.
2. **Python Hooks**: Execute custom Python code by providing a path to a Python file.

### Basic Configuration

```yaml
hooks:
  start:
    - name: log_session_start
      command: "echo 'Session started at {timestamp}' >> .cecli/hooks_log.txt"
      priority: 10
      enabled: true
      description: "Logs session start to file"
```

## Hook Types

The following hook types are available:

| Hook Type | Trigger Point |
|-----------|---------------|
| `start` | When the agent session begins. |
| `on_message` | When a new user message is received. |
| `end_message` | When message processing completes. |
| `pre_tool` | Before a tool is executed. |
| `post_tool` | After a tool execution completes. |
| `end` | When the agent session ends. |

## Configuration Options

Each hook entry supports the following options:

- `name`: (Required) A unique name for the hook.
- `command`: The shell command to execute (for Command Hooks).
- `file`: The path to a Python file (for Python Hooks).
- `priority`: (Optional) Execution order (lower numbers run first). Default is 10.
- `enabled`: (Optional) Whether the hook is active. Default is true.
- `description`: (Optional) A brief description of what the hook does.

## Command Hooks

Command hooks are simple shell commands. You can use placeholders in the command string that will be replaced with metadata from the hook event.

### Available Metadata

| Hook Type | Available Placeholders |
|-----------|------------------------|
| `start`, `end` | `{timestamp}` `{coder_type}` |
| `on_message` `end_message` | `{timestamp}` `{message}` `{message_length}` |
| `pre_tool` | `{timestamp}` `{tool_name}` `{arg_string}` |
| `post_tool` | `{timestamp}` `{tool_name}` `{arg_string}` `{output}` |

### Example: Aborting Tool Execution
If a `pre_tool` command hook returns a non-zero exit code, the tool execution will be aborted.

```yaml
hooks:
  pre_tool:
    - name: check_dangerous_command
      command: "python3 scripts/check_safety.py --args '{arg_string}'"
```

## Python Hooks

Python hooks allow for more complex logic. To create a Python hook, create a `.py` file and define a class that inherits from `BaseHook`.

### Example Python Hook

**File**: `.cecli/hooks/my_hook.py`
```python
from cecli.hooks import BaseHook
from cecli.hooks.types import HookType

class MyCustomHook(BaseHook):
    type = HookType.PRE_TOOL
    
    async def execute(self, coder, metadata):
        # Access coder instance or metadata
        tool_name = metadata.get("tool_name")
        
        if tool_name == "delete_file":
            print("Warning: Deleting a file!")
            
        # Return False to abort operation (for pre_tool/post_tool)
        return True
```

**Configuration**:
```yaml
hooks:
  pre_tool:
    - name: my_custom_python_hook
      file: .cecli/hooks/my_hook.py
```

## Managing Hooks

You can manage hooks during an active session using the following slash commands:

- `/hooks`: List all registered hooks and their status.
- `/load-hook <name>`: Enable a specific hook.
- `/remove-hook <name>`: Disable a specific hook.

## Best Practices

- **Security**: Hooks run with the same permissions as `cecli`. Be careful when running scripts from untrusted sources.
- **Performance**: Avoid long-running tasks in hooks, as they can block the agent's loop.
- **Error Handling**: If a hook fails, `cecli` will generally log the error and continue, except for `pre_tool` hooks which can abort execution.
