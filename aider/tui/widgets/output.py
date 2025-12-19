"""Output widget for Aider TUI using Textual's RichLog widget."""

import re

from rich.padding import Padding
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events, on
from textual.message import Message
from textual.widgets import RichLog


class CostUpdate(Message):
    """Message to update cost in footer."""

    def __init__(self, cost: float):
        self.cost = cost
        super().__init__()


class OutputContainer(RichLog):
    """Scrollable output area using RichLog widget for rich rendering.

    Uses Textual's RichLog widget for efficient streaming and display
    of LLM responses and system messages.
    """

    DEFAULT_CSS = """
    OutputContainer {
        scrollbar-gutter: stable;
        background: $surface;
        padding: 0 0;
    }
    """

    _last_write_type = None
    _write_history = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Line buffer for streaming text to avoid word-per-line issue
        self._line_buffer = ""

        # Enable markup for rich formatting
        self.highlight = True
        self.markup = True
        self.wrap = True

    async def start_response(self):
        """Start a new LLM response section with streaming support."""
        # Clear the line buffer for new response
        self._line_buffer = ""

    async def stream_chunk(self, text: str):
        """Stream a chunk of markdown text."""
        if not text:
            return

        # Check for cost updates in the text
        self._check_cost(text)

        # Add text to line buffer
        self._line_buffer += text

        # Process complete lines from buffer
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            # self.write(Padding(line.strip(), (0, 0, 0, 1)))
            if line.rstrip():
                self.set_last_write_type("assistant")
                self.output(line.rstrip())

    async def end_response(self):
        """End the current LLM response."""
        await self._stop_stream()

    async def _stop_stream(self):
        """Stop the current markdown stream."""
        # Flush any remaining buffer content
        if self._line_buffer.rstrip():
            self.output(self.rstrip())
            self._line_buffer = ""

    def add_user_message(self, text: str):
        """Add a user message (displayed differently from LLM output)."""
        # User messages shown with > prefix in green color
        self.auto_scroll = True
        self.set_last_write_type("user")
        self.output(f"[bold medium_spring_green]> {text}[/bold medium_spring_green]")
        self.scroll_end(animate=False)

    def add_system_message(self, text: str, dim=True):
        """Add a system/tool message."""
        if not text.strip():
            return

        # Escape any Rich markup brackets
        text = text.removesuffix("\n")
        start = ""
        end = ""
        # Write system message in secondary color
        if dim:
            start = "[dim]"
            end = "[/dim]"
            text = Padding(f"{start}{text}{end}", (0, 0, 0, 2))

        self.set_last_write_type("system")
        self.output(text)

    def add_output(self, text: str, task_id: str = None, dim=True):
        """Add output text as a system message.

        This handles tool output, status messages, etc.
        LLM streaming is handled separately via start_response/stream_chunk/end_response.
        """
        if not text:
            return

        # Check for cost updates
        self._check_cost(text)

        # Always treat add_output as system messages
        # LLM streaming goes through the dedicated stream_chunk path
        self.add_system_message(text, dim=dim)

    def add_output_styled(self, text: str, styles=None):
        if not styles:
            styles = dict()

        style = RichStyle(**styles)
        with self.app.console.capture() as capture:
            self.app.console.print(Text(text), style=style)
        capture_text = capture.get()

        self.output(Padding(capture_text, (0, 0, 0, 2)))

    def _check_cost(self, text: str):
        """Extract and emit cost updates."""
        match = re.search(r"\$(\d+\.?\d*)\s*session", text)
        if match:
            try:
                self.post_message(CostUpdate(float(match.group(1))))
            except (ValueError, AttributeError):
                pass

    def start_task(self, task_id: str, title: str, task_type: str = "general"):
        """Start a new task section."""
        self.set_last_write_type(f"{task_id}-{title}-{task_type}")

    def clear_output(self):
        """Clear all output."""
        self._line_buffer = ""
        self.clear()

    def set_last_write_type(self, type):
        if type and self._last_write_type and self._last_write_type != type:
            self.output("")

        self._last_write_type = type

    def output(self, text, check_duplicates=True):
        """Write output with duplicate newline checking.

        Args:
            text: The text to write
            check_duplicates: If True, check for duplicate newlines before writing
        """
        with self.app.console.capture() as capture:
            self.app.console.print(text)
        check = Text(capture.get()).plain

        # self.write(str(self._write_history))
        # self.write(repr(check))

        # Check for duplicate newlines

        if check_duplicates and len(self._write_history) >= 2:
            nl_check = check in ["", "\n", "\\n"]
            nl_last = self._write_history[-1] in ["", "\n", "\\n"]
            nl_penultimate = self._write_history[-2] in ["", "\n", "\\n"] or self._write_history[
                -2
            ].endswith("\n")

            if nl_check and nl_last and nl_penultimate:
                return

        # Call the actual write method
        self.write(text)

        # Log the write
        self._write_history.append(check)

        # Keep history size manageable
        if len(self._write_history) > 5:
            self._write_history.pop(0)

    @on(events.Print)
    def log_print(self, event: events.Print) -> None:
        """Writes the captured print output to the RichLog widget."""
        if event.text.strip():
            theme_vars = self.app.get_css_variables()
            color = theme_vars.get("warning")
            write_type = "stdout"

            if event.stderr:
                color = theme_vars.get("error")
                write_type = "stderr"

            self.set_last_write_type(write_type)
            self.add_output_styled(event.text.removesuffix("\n"), {"color": color})

        # Prevent the event from bubbling further
        event.prevent_default()

    @on(events.MouseScrollUp)
    def disable_auto_scroll(self, event: events.MouseScrollUp) -> None:
        """
        Event handler called when the screen is scrolled up.
        Disables automatic scrolling
        """
        self.auto_scroll = False

    @on(events.MouseScrollDown)
    def enable_auto_scroll(self, event: events.MouseScrollDown) -> None:
        """
        Event handler called when the screen is scrolled down.
        Enables automatic scrolling if we are near the end
        """

        # Calculate the relevant dimensions
        scroll_top = self.scroll_y
        view_height = self.size.height
        content_height = self.content_size.height

        # Check if scrolled to the bottom (allowing for minor floating point inaccuracies)
        if scroll_top + view_height >= content_height - 32:
            self.auto_scroll = True
