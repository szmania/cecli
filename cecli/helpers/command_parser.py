from typing import List


def split_shell_commands(commands_str: str) -> List[str]:
    """
    Split shell commands on semicolons, respecting quoted strings and heredoc boundaries.

    Args:
        commands_str: String containing shell commands separated by semicolons

    Returns:
        List of command strings split at semicolons outside quotes/heredocs
    """
    if not commands_str:
        return []

    commands: List[str] = []
    current_command = []

    # State tracking
    in_single_quote = False
    in_double_quote = False
    in_heredoc = False
    heredoc_delimiter = ""
    heredoc_ignore_tabs = False  # True for <<-EOF (dash before delimiter)
    heredoc_seen_newline = False  # Have we seen a newline since entering heredoc?
    escaped = False

    i = 0
    n = len(commands_str)

    while i < n:
        char = commands_str[i]

        if escaped:
            # Current character is escaped, treat it as literal
            current_command.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\":
            # Escape character
            escaped = True
            current_command.append(char)
            i += 1
            continue

        if not in_heredoc:
            # Not in heredoc, handle quotes and semicolons
            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current_command.append(char)
            elif char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current_command.append(char)
            elif char == ";" and not in_single_quote and not in_double_quote:
                # Split at semicolon outside quotes
                command = "".join(current_command).strip()
                if command:
                    commands.append(command)
                current_command = []
            else:
                # Check for heredoc start (<<)
                if char == "<" and i + 1 < n and commands_str[i + 1] == "<":
                    # Remember position for capturing the heredoc start
                    heredoc_start_pos = i
                    i += 2  # Skip "<<"

                    # Check for dash (<<-)
                    if i < n and commands_str[i] == "-":
                        heredoc_ignore_tabs = True
                        i += 1

                    # Skip whitespace before delimiter
                    while i < n and commands_str[i] in (" ", "\t"):
                        i += 1

                    # Parse delimiter (can be quoted or unquoted)
                    delimiter_start = i

                    # Check for quotes
                    if i < n and commands_str[i] in ("'", '"'):
                        quote_char = commands_str[i]
                        i += 1
                        delimiter_start = i

                        # Find closing quote
                        while i < n and commands_str[i] != quote_char:
                            i += 1

                        heredoc_delimiter = commands_str[delimiter_start:i]
                        if i < n:
                            i += 1  # Skip closing quote
                    else:
                        # Unquoted delimiter - alphanumeric and underscore only
                        while i < n and (commands_str[i].isalnum() or commands_str[i] == "_"):
                            i += 1
                        heredoc_delimiter = commands_str[delimiter_start:i]

                    if heredoc_delimiter:
                        # Valid heredoc found - capture the entire heredoc start
                        heredoc_start_text = commands_str[heredoc_start_pos:i]
                        current_command.append(heredoc_start_text)

                        # Enter heredoc mode
                        in_heredoc = True
                        heredoc_seen_newline = False
                        continue
                    else:
                        # Not a valid heredoc, treat as normal "<<"
                        current_command.append("<<")
                        continue
                else:
                    current_command.append(char)
        else:
            # Inside heredoc
            current_command.append(char)

            # Track if we've seen a newline (heredoc content starts after newline)
            if char == "\n":
                heredoc_seen_newline = True

                # Look ahead to see if next line starts with delimiter
                j = i + 1

                # Skip leading whitespace (tabs if heredoc_ignore_tabs)
                while j < n and commands_str[j] in (" ", "\t"):
                    if heredoc_ignore_tabs and commands_str[j] == "\t":
                        j += 1
                    elif not heredoc_ignore_tabs:
                        j += 1
                    else:
                        break

                # Check if we have the delimiter at this position
                if (
                    j + len(heredoc_delimiter) <= n
                    and commands_str[j : j + len(heredoc_delimiter)] == heredoc_delimiter
                ):
                    # Check what comes after delimiter
                    k = j + len(heredoc_delimiter)
                    if k >= n or commands_str[k] in (" ", "\t", "\n", "\r", ";"):
                        # Found heredoc end
                        in_heredoc = False
                        heredoc_delimiter = ""
                        heredoc_ignore_tabs = False
                        heredoc_seen_newline = False
            elif char == ";" and not heredoc_seen_newline:
                # Semicolon before seeing a newline in heredoc mode
                # This means heredoc start was immediately followed by semicolon
                # Not a real heredoc - split at this semicolon
                in_heredoc = False
                heredoc_delimiter = ""
                heredoc_ignore_tabs = False
                heredoc_seen_newline = False

                # Remove the semicolon from current_command
                current_command.pop()

                # Split at this semicolon
                command = "".join(current_command).strip()
                if command:
                    commands.append(command)
                current_command = []
                continue

        i += 1

    # Add the last command if any
    if escaped:
        # Handle trailing escape character
        current_command.append("\\")

    command = "".join(current_command).strip()
    if command:
        commands.append(command)

    return commands
