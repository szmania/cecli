from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class RemoveMcpCommand(BaseCommand):
    NORM_NAME = "remove-mcp"
    DESCRIPTION = "Remove a MCP server by name"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the remove-mcp command with given parameters."""
        if not args.strip():
            return format_command_result(io, cls.NORM_NAME, "Usage: /remove-mcp <mcp-name>")

        if not coder.mcp_manager or not coder.mcp_manager.servers:
            return format_command_result(
                io, cls.NORM_NAME, "No MCP servers connected, nothing to remove."
            )

        server_name = args.strip()
        import asyncio
        coder.interrupt_event.clear()
        disconnect_task = asyncio.create_task(coder.mcp_manager.disconnect_server(server_name))
        interrupt_task = asyncio.create_task(coder.interrupt_event.wait())

        done, pending = await asyncio.wait(
            {disconnect_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if interrupt_task in done:
            disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
            io.tool_warning("MCP disconnection interrupted.")
            return

        was_disconnected = disconnect_task.result()

        if not was_disconnected:
            return format_command_result(
                io, cls.NORM_NAME, "", f"Unable to remove server: {server_name}"
            )

        io.tool_output(f"Removed server: {server_name}")

        from . import SwitchCoderSignal
        raise SwitchCoderSignal(
            edit_format=coder.edit_format,
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=True,
            mcp_manager=coder.mcp_manager,
        )

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for remove-mcp command."""
        if not coder.mcp_manager or not coder.mcp_manager.servers:
            return []

        try:
            server_names = [server.name for server in coder.mcp_manager if server.is_connected]
            return server_names
        except Exception:
            return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the remove-mcp command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /remove-mcp <mcp-name>  # Remove a mcp by name\n"
        help_text += "\nExamples:\n"
        help_text += "  /remove-mcp context7  # Remove the context7 mcp\n"
        help_text += "  /remove-mcp github  # Remove the github mcp\n"
        help_text += "\nThis command removes a MCP server by name.\n"
        return help_text
