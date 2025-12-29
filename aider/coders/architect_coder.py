import asyncio

from ..commands import SwitchCoder
from .architect_prompts import ArchitectPrompts
from .base_coder import Coder


class ArchitectCoder(Coder):
    edit_format = "architect"
    gpt_prompts = ArchitectPrompts()
    auto_accept_architect = False
    max_tool_calls = 5

    async def process_tool_calls(self, tool_call_response):
        return await super().process_tool_calls(tool_call_response)

    async def reply_completed(self):
        content = self.partial_response_content

        if not content or not content.strip():
            return

        tweak_responses = getattr(self.args, "tweak_responses", False)
        confirmation = await self.io.confirm_ask("Edit the files?", allow_tweak=tweak_responses)

        if not self.auto_accept_architect and not confirmation:
            return

        if confirmation == "tweak":
            content = self.io.edit_in_editor(content)

        await asyncio.sleep(0.1)

        kwargs = dict()

        # Use the editor_model from the main_model if it exists, otherwise use the main_model itself
        editor_model = self.main_model.editor_model or self.main_model

        kwargs["main_model"] = editor_model
        kwargs["edit_format"] = self.main_model.editor_edit_format
        kwargs["args"] = self.args
        kwargs["suggest_shell_commands"] = False
        kwargs["map_tokens"] = 0
        kwargs["total_cost"] = self.total_cost
        kwargs["cache_prompts"] = False
        kwargs["num_cache_warming_pings"] = 0
        kwargs["summarize_from_coder"] = False
        kwargs["mcp_servers"] = []  # Empty to skip initialization

        coder = await Coder.create(**kwargs)
        # Transfer MCP state to avoid re-initialization
        coder.mcp_servers = self.mcp_servers
        coder.mcp_tools = self.mcp_tools
        # Transfer TUI app weak reference
        coder.tui = self.tui

        new_kwargs = dict(io=self.io, from_coder=self)
        new_kwargs.update(kwargs)

        editor_coder = await Coder.create(**new_kwargs)
        editor_coder.cur_messages = []
        editor_coder.done_messages = []

        if self.verbose:
            editor_coder.show_announcements()

        try:
            await editor_coder.generate(user_message=content, preproc=False)
            self.move_back_cur_messages("I made those changes to the files.")
            self.total_cost = editor_coder.total_cost
            self.aider_commit_hashes = editor_coder.aider_commit_hashes
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception as e:
            self.io.tool_error(e)

        raise SwitchCoder(main_model=self.main_model, edit_format="architect")
