from typing import List

from cecli.commands.utils.base_command import BaseCommand
from cecli.commands.utils.helpers import format_command_result


class LoadMcpCommand(BaseCommand):
    NORM_NAME = "load-mcp"
    DESCRIPTION = "Load a MCP server by name"

    @classmethod
    async def execute(cls, io, coder, args, **kwargs):
        """Execute the load-mcp command with given parameters."""
        if not args.strip():
            return format_command_result(io, cls.NORM_NAME, "Usage: /load-mcp <mcp-name>")

        if not coder.mcp_manager or not coder.mcp_manager.servers:
            return format_command_result(
                io, cls.NORM_NAME, "No MCP servers found, nothing to load."
            )

        server_name = args.strip()
        server = coder.mcp_manager.get_server(server_name)
        if server is None:
            return format_command_result(
                io, cls.NORM_NAME, "", f"MCP server {server_name} does not exist."
            )

        import asyncio
        coder.interrupt_event.clear()
        connect_task = asyncio.create_task(coder.mcp_manager.connect_server(server.name))
        interrupt_task = asyncio.create_task(coder.interrupt_event.wait())

        done, pending = await asyncio.wait(
            {connect_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if interrupt_task in done:
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
            io.tool_warning("MCP connection interrupted.")
            return

        did_connect = connect_task.result()

        if not did_connect:
            return format_command_result(io, cls.NORM_NAME, "", f"Unable to load server: {server_name}")

        io.tool_output(f"Loaded server: {server_name}")

        from . import SwitchCoderSignal
        raise SwitchCoderSignal(
            edit_format=coder.edit_format,
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=True,
        )

    @classmethod
    def get_completions(cls, io, coder, args) -> List[str]:
        """Get completion options for load-mcp command."""
        if not coder.mcp_manager or not coder.mcp_manager.servers:
            return []

        try:
            server_names = [
                server.name
                for server in coder.mcp_manager
                if server not in coder.mcp_manager.connected_servers
            ]
            return server_names
        except Exception:
            return []

    @classmethod
    def get_help(cls) -> str:
        """Get help text for the load-mcp command."""
        help_text = super().get_help()
        help_text += "\nUsage:\n"
        help_text += "  /load-mcp <mcp-name>  # Load a mcp by name\n"
        help_text += "\nExamples:\n"
        help_text += "  /load-mcp context7  # Load the context7 mcp\n"
        help_text += "  /load-mcp github  # Load the github mcp\n"
        help_text += "\nThis command loads a MCP server by name.\n"
        return help_text
