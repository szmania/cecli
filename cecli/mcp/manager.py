import asyncio

from cecli.llm import litellm
from cecli.mcp.server import LocalServer, McpServer
from cecli.tools.utils.registry import ToolRegistry


class McpServerManager:
    """
    Centralized manager for MCP server connections.

    Handles connection lifecycle for all MCP servers, ensuring
    connections are established once and reused across all Coder instances.
    """

    def __init__(
        self,
        servers: list[McpServer],
        io=None,
        verbose: bool = False,
    ):
        """
        Initialize the MCP server manager.

        Args:
            servers: List of MCP Servers to manage
            io: InputOutput instance for user interaction
            verbose: Whether to output verbose logging
        """
        self.io = io
        self.verbose = verbose
        self._servers = servers

        self._server_tools: dict[str, list] = {}  # Maps server name to its tools
        self._connected_servers: set[McpServer] = set()

    def _log_verbose(self, message: str) -> None:
        """Log a verbose message if verbose mode is enabled and IO is available."""
        if self.verbose and self.io:
            self.io.tool_output(message)

    def _log_error(self, message: str) -> None:
        """Log an error message if IO is available."""
        if self.io:
            self.io.tool_error(message)

    def _log_warning(self, message: str) -> None:
        """Log a warning message if IO is available."""
        if self.io:
            self.io.tool_warning(message)

    @property
    def servers(self) -> list["McpServer"]:
        """Get the list of managed MCP servers."""
        return self._servers

    @property
    def is_connected(self) -> bool:
        """Check if any servers are connected."""
        return len(self._connected_servers) > 0

    def get_server(self, name: str) -> McpServer | None:
        """
        Get a server by name.

        Args:
            name: Name of the server to retrieve

        Returns:
            The server instance or None if not found
        """
        try:
            return next(server for server in self._servers if server.name == name)
        except StopIteration:
            return None

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        if not self._connected_servers:
            self._log_verbose("MCP servers already disconnected")
            return

        self._log_verbose("Disconnecting from all MCP servers")

        async def disconnect_server(server: McpServer) -> tuple[McpServer, bool]:
            try:
                await server.disconnect()
                if server.name in self._server_tools:
                    del self._server_tools[server.name]
                self._log_verbose(f"Disconnected from MCP server: {server.name}")
                return (server, True)
            except Exception:
                self._log_warning(f"Error disconnected from MCP server: {server.name}")
                return (server, False)

        # Create a copy to avoid modifying during iteration
        servers_to_disconnect = list(self._connected_servers)
        tasks = [disconnect_server(server) for server in servers_to_disconnect]
        results = await asyncio.gather(*tasks)

        for server, success in results:
            if success:
                self._connected_servers.remove(server)

    async def connect_server(self, name: str) -> bool:
        """
        Connect to a specific MCP server by name.

        Args:
            name: Name of the server to connect to

        Returns:
            Boolean indicating success or failure
        """
        server = self.get_server(name)
        if not server:
            self._log_warning(f"MCP server not found: {name}")
            return False

        if server in self._connected_servers:
            self._log_verbose(f"MCP server already connected: {name}")
            return True

        # We will handle local server differently since its only used for internal usage
        # We'll pretend we connect and fetched all tools
        if isinstance(server, LocalServer):
            await server.connect()
            self._connected_servers.add(server)
            self._server_tools[server.name] = get_local_tool_schemas()
            return True

        try:
            session = await server.connect()
            tools = await litellm.experimental_mcp_client.load_mcp_tools(
                session=session, format="openai"
            )
            self._server_tools[server.name] = tools
            self._connected_servers.add(server)
            self._log_verbose(f"Connected to MCP server: {name}")
            return True
        except (Exception, asyncio.CancelledError) as e:
            if server.name != "unnamed-server":
                self._log_error(f"Failed to connect to MCP server {name}: {e}")
            return False

    async def disconnect_server(self, name: str) -> bool:
        """
        Disconnect from a specific MCP server by name.

        Args:
            name: Name of the server to disconnect from

        Returns:
            Boolean indicating success or failure
        """
        server = self.get_server(name)
        if not server:
            self._log_warning(f"MCP server not found: {name}")
            return False

        if server not in self._connected_servers:
            self._log_verbose(f"MCP server not connected: {name}")
            return True

        try:
            await server.disconnect()
            if server.name in self._server_tools:
                del self._server_tools[server.name]
            self._connected_servers.remove(server)
            self._log_verbose(f"Disconnected from MCP server: {name}")
            return True
        except Exception as e:
            self._log_warning(f"Error disconnecting from MCP server {name}: {e}")
            return False

    async def add_server(self, server: McpServer, connect: bool = False) -> bool:
        """
        Add a new MCP server to the manager.

        Args:
            server: McpServer instance to add
            connect: Whether to immediately connect to the server

        Returns:
            Boolean indicating success or failure
        """
        existing_server = self.get_server(server.name)
        if existing_server:
            if server.name not in ["unnamed-server", "Local"]:
                self._log_warning(f"MCP server with name '{server.name}' already exists")
            return False

        self._servers.append(server)
        self._log_verbose(f"Added MCP server: {server.name}")

        if connect:
            return await self.connect_server(server.name)

        return True

    @property
    def connected_servers(self) -> list["McpServer"]:
        """Get the list of successfully connected servers."""
        return list(self._connected_servers)

    @property
    def failed_servers(self) -> list["McpServer"]:
        """Get the list of servers that failed to connect."""
        return [server for server in self._servers if server not in self._connected_servers]

    def __iter__(self):
        for server in self._servers:
            yield server

    def get_server_tools(self, name: str) -> list:
        """
        Get the tools for a specific server.

        Args:
            name: Name of the server

        Returns:
            List of tools or empty list if server not found or not connected
        """
        return self._server_tools.get(name, list())

    @property
    def all_tools(self) -> dict[str, list]:
        """
        Get all tools from all connected servers.

        Returns:
            Dictionary mapping server names to their tools
        """
        return self._server_tools.copy()

    @classmethod
    async def from_servers(
        cls, servers: list[McpServer], io=None, verbose: bool = False
    ) -> "McpServerManager":
        """
        Create an MCP Server Manager from a list of servers it should manage.
        Automatically connects if the server is set to auto connect (by default it is)
        """
        mcp_manager = cls(servers=[], io=io, verbose=verbose)

        async def add_server_with_retry(
            server: McpServer, connect: bool = True, max_retries: int = 3
        ) -> tuple[McpServer, bool]:
            """Try to add and connect to a server with retries."""
            if not connect:
                success = await mcp_manager.add_server(server, connect=False)
                return (server, success)

            for _attempt in range(max_retries):
                success = await mcp_manager.add_server(server, connect=True)
                if success:
                    return (server, True)
            return (server, False)

        tasks = []
        for server in servers:
            auto_connect = server.config.get("enabled", True)
            tasks.append(add_server_with_retry(server, connect=auto_connect))

        results = await asyncio.gather(*tasks)
        for server, did_connect in results:
            if not did_connect and server.name not in ["unnamed-server", "Local"]:
                io.tool_warning(
                    f"MCP tool initialization failed after multiple retries: {server.name}"
                )

        if verbose:
            io.tool_output("MCP servers configured:")

            for server, _ in results:
                io.tool_output(f"  - {server.name}")

                for tool in mcp_manager.get_server_tools(server.name):
                    tool_name = tool.get("function", {}).get("name", "unknown")
                    tool_desc = tool.get("function", {}).get("description", "").split("\n")[0]
                    io.tool_output(f"    - {tool_name}: {tool_desc}")

        return mcp_manager


def get_local_tool_schemas():
    """Returns the JSON schemas for all local tools using the tool registry."""
    schemas = []
    for tool_name in ToolRegistry.get_registered_tools():
        tool_module = ToolRegistry.get_tool(tool_name)
        if hasattr(tool_module, "SCHEMA"):
            schemas.append(tool_module.SCHEMA)
    return schemas
