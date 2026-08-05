"""
WaddleAI MCP Server
Standalone Model Context Protocol server for external applications
"""

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import asyncio

import structlog

from shared.auth.penguin_auth import create_oidc_provider
from shared.auth.rbac import RBACManager
from shared.database.models import get_db, init_default_data
from shared.utils.llm_connectors import create_llm_connection_manager
from shared.utils.mcp_interface import create_mcp_server
from shared.utils.request_router import create_request_router

# Configure structured logging
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="ISO"), structlog.processors.JSONRenderer()],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class MCPServerApp:
    """MCP Server Application"""

    def __init__(self):
        self.db = None
        self.rbac = None
        self.oidc_provider = None
        self.llm_manager = None
        self.request_router = None
        self.mcp_server = None
        self.websocket_server = None

        # Configuration
        self.config = {
            "mcp_host": os.getenv("MCP_HOST", "localhost"),
            "mcp_port": int(os.getenv("MCP_PORT", "8765")),
        }

    async def startup(self):
        """Initialize MCP server components"""
        logger.info("Starting WaddleAI MCP Server")

        # Initialize database
        self.db = get_db()
        init_default_data(self.db)

        # Initialize components
        self.rbac = RBACManager(self.db)
        self.oidc_provider = create_oidc_provider()
        self.llm_manager = create_llm_connection_manager(self.db)
        self.request_router = create_request_router(self.llm_manager, self.db)

        # Create MCP server
        self.mcp_server = create_mcp_server(self.rbac, self.request_router, self.db, self.oidc_provider)

        # Start WebSocket server
        self.websocket_server = await self.mcp_server.start_server(
            host=self.config["mcp_host"], port=self.config["mcp_port"]
        )

        logger.info(f"MCP server started on ws://{self.config['mcp_host']}:{self.config['mcp_port']}")

    async def shutdown(self):
        """Cleanup MCP server components"""
        logger.info("Shutting down MCP server")

        if self.websocket_server:
            self.websocket_server.close()
            await self.websocket_server.wait_closed()

        if self.llm_manager:
            await self.llm_manager.close_all()

        logger.info("MCP server shutdown complete")

    async def run(self):
        """Run MCP server"""
        try:
            await self.startup()

            # Keep server running
            await asyncio.Future()  # Run forever

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
        finally:
            await self.shutdown()


async def main():
    """Main entry point"""
    app = MCPServerApp()
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMCP server stopped")
