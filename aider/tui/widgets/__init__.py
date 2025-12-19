"""Widgets for the Aider TUI."""

from .completion_bar import CompletionBar
from .file_list import FileList
from .footer import AiderFooter
from .input_area import InputArea
from .key_hints import KeyHints
from .output import OutputContainer
from .status_bar import StatusBar

__all__ = [
    "AiderFooter",
    "CompletionBar",
    "InputArea",
    "KeyHints",
    "OutputContainer",
    "StatusBar",
    "FileList",
]
