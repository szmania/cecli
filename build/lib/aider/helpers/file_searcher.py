"""
File search utilities for aider.

This module provides functions for searching and resolving file paths
relative to various directories (git root, home folder, .aider, .cecli, etc.).
"""

from pathlib import Path
from typing import List, Optional


def generate_search_path_list(
    default_file: str, git_root: Optional[str], command_line_file: Optional[str]
) -> List[str]:
    """
    Generate a list of file paths to search for configuration files.

    The search order is:
    1. Home directory (~/default_file)
    2. Git root directory (git_root/default_file) if git_root is provided
    3. Current directory (default_file)
    4. Command line specified file (command_line_file) if provided

    Args:
        default_file: The default filename to search for
        git_root: The git root directory (optional)
        command_line_file: A file specified on the command line (optional)

    Returns:
        List of resolved file paths in search order (first to last)
    """
    files = []
    files.append(Path.home() / default_file)  # homedir
    if git_root:
        files.append(Path(git_root) / default_file)  # git root
    files.append(default_file)
    if command_line_file:
        files.append(command_line_file)

    resolved_files = []
    for fn in files:
        try:
            resolved_files.append(Path(fn).expanduser().resolve())
        except OSError:
            pass

    files = resolved_files
    files.reverse()
    uniq = []
    for fn in files:
        if fn not in uniq:
            uniq.append(fn)
    uniq.reverse()
    files = uniq
    files = list(map(str, files))
    files = list(dict.fromkeys(files))

    return files


def resolve_file_path(
    filename: str,
    relative_to: str = "auto",
    git_root: Optional[str] = None,
    search_dirs: Optional[List[str]] = None,
) -> Optional[Path]:
    """
    Resolve a file path relative to various directories.

    Args:
        filename: The filename to resolve
        relative_to: Where to resolve the file from. Options:
            - "auto": Try multiple locations (git_root, home, .aider, .cecli, cwd)
            - "git": Resolve relative to git root
            - "home": Resolve relative to home directory
            - "cwd": Resolve relative to current working directory
            - "aider": Resolve relative to .aider directory in git root or home
            - "cecli": Resolve relative to .cecli directory in git root or home
        git_root: The git root directory (optional, required for some modes)
        search_dirs: Additional directories to search (optional)

    Returns:
        Resolved Path object if found, None otherwise
    """
    if relative_to == "auto":
        # Try multiple locations in order of preference
        locations = []

        # 1. Git root (if available)
        if git_root:
            locations.append(Path(git_root) / filename)

        # 2. Home directory
        locations.append(Path.home() / filename)

        # 3. .aider directories
        if git_root:
            locations.append(Path(git_root) / ".aider" / filename)
        locations.append(Path.home() / ".aider" / filename)

        # 4. .cecli directories
        if git_root:
            locations.append(Path(git_root) / ".cecli" / filename)
        locations.append(Path.home() / ".cecli" / filename)

        # 5. Current working directory
        locations.append(Path.cwd() / filename)

        # 6. Additional search directories
        if search_dirs:
            for dir_path in search_dirs:
                locations.append(Path(dir_path) / filename)

        # Try each location
        for location in locations:
            try:
                resolved = location.expanduser().resolve()
                if resolved.exists():
                    return resolved
            except OSError:
                continue

        return None

    elif relative_to == "git":
        if not git_root:
            raise ValueError("git_root is required when relative_to='git'")
        return (Path(git_root) / filename).expanduser().resolve()

    elif relative_to == "home":
        return (Path.home() / filename).expanduser().resolve()

    elif relative_to == "cwd":
        return (Path.cwd() / filename).expanduser().resolve()

    elif relative_to == "aider":
        # Try git root first, then home
        if git_root:
            aider_path = Path(git_root) / ".aider" / filename
            try:
                resolved = aider_path.expanduser().resolve()
                if resolved.exists():
                    return resolved
            except OSError:
                pass

        # Fall back to home directory
        return (Path.home() / ".aider" / filename).expanduser().resolve()

    elif relative_to == "cecli":
        # Try git root first, then home
        if git_root:
            cecli_path = Path(git_root) / ".cecli" / filename
            try:
                resolved = cecli_path.expanduser().resolve()
                if resolved.exists():
                    return resolved
            except OSError:
                pass

        # Fall back to home directory
        return (Path.home() / ".cecli" / filename).expanduser().resolve()

    else:
        raise ValueError(f"Invalid relative_to value: {relative_to}")


def find_config_file(
    config_name: str,
    git_root: Optional[str] = None,
    command_line_file: Optional[str] = None,
    config_dirs: Optional[List[str]] = None,
) -> Optional[Path]:
    """
    Find a configuration file using the standard search path.

    This is a higher-level function that uses generate_search_path_list
    to find configuration files.

    Args:
        config_name: The configuration filename (e.g., ".env", ".aider.conf.yml")
        git_root: The git root directory (optional)
        command_line_file: A file specified on the command line (optional)
        config_dirs: Additional directories to search (optional)

    Returns:
        Path to the first existing configuration file found, or None
    """
    # Generate standard search path
    search_paths = generate_search_path_list(config_name, git_root, command_line_file)

    # Add additional config directories if provided
    if config_dirs:
        for dir_path in config_dirs:
            search_paths.append(str(Path(dir_path) / config_name))

    # Check each path
    for file_path in search_paths:
        path_obj = Path(file_path)
        if path_obj.exists():
            return path_obj.resolve()

    return None
