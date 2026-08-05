"""
Example MCP client for WaddleAI
Demonstrates how to connect and use WaddleAI via MCP protocol
"""

import asyncio
import json
from typing import Any, Dict

import websockets


class WaddleAIMCPClient:
    """MCP client for WaddleAI"""

    def __init__(self, uri: str, api_key: str):
        self.uri = uri
        self.api_key = api_key
        self.websocket = None
        self.message_id = 0

    async def connect(self):
        """Connect to WaddleAI MCP server"""
        print(f"Connecting to {self.uri}...")
        self.websocket = await websockets.connect(self.uri)

        # Authenticate
        await self._send_auth()
        auth_response = await self._receive_message()

        if auth_response.get("result", {}).get("authenticated"):
            print("✓ Authentication successful")
            user_info = auth_response["result"]["user"]
            print(f"  Logged in as: {user_info['username']} ({user_info['role']})")

            # Initialize MCP session
            await self._initialize()
            return True
        else:
            print("✗ Authentication failed")
            return False

    async def disconnect(self):
        """Disconnect from server"""
        if self.websocket:
            await self.websocket.close()
            print("Disconnected from server")

    async def _send_auth(self):
        """Send authentication message"""
        auth_message = {"api_key": self.api_key}
        await self.websocket.send(json.dumps(auth_message))

    async def _initialize(self):
        """Initialize MCP session"""
        init_message = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": {"name": "WaddleAI Example Client", "version": "1.0.0"},
            },
        }

        await self.websocket.send(json.dumps(init_message))
        response = await self._receive_message()

        if "result" in response:
            print("✓ MCP session initialized")
            server_info = response["result"]["serverInfo"]
            print(f"  Server: {server_info['name']} v{server_info['version']}")
        else:
            print("✗ MCP initialization failed")

    async def list_tools(self) -> Dict[str, Any]:
        """List available tools"""
        message = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"}

        await self.websocket.send(json.dumps(message))
        response = await self._receive_message()
        return response.get("result", {})

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool"""
        message = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        await self.websocket.send(json.dumps(message))
        response = await self._receive_message()
        return response

    async def list_resources(self) -> Dict[str, Any]:
        """List available resources"""
        message = {"jsonrpc": "2.0", "id": self._next_id(), "method": "resources/list"}

        await self.websocket.send(json.dumps(message))
        response = await self._receive_message()
        return response.get("result", {})

    async def read_resource(self, uri: str) -> Dict[str, Any]:
        """Read a resource"""
        message = {"jsonrpc": "2.0", "id": self._next_id(), "method": "resources/read", "params": {"uri": uri}}

        await self.websocket.send(json.dumps(message))
        response = await self._receive_message()
        return response

    async def chat(self, messages: list, model: str = "gpt-3.5-turbo", **kwargs) -> str:
        """Generate chat completion"""
        arguments = {"messages": messages, "model": model, **kwargs}

        response = await self.call_tool("chat_completion", arguments)

        if "result" in response:
            # Extract response text from MCP result
            content = response["result"]["content"][0]["text"]
            result_data = json.loads(content)
            return result_data["response"]
        elif "error" in response:
            raise Exception(f"Chat completion failed: {response['error']['message']}")
        else:
            raise Exception("Unexpected response format")

    def _next_id(self) -> int:
        """Get next message ID"""
        self.message_id += 1
        return self.message_id

    async def _receive_message(self) -> Dict[str, Any]:
        """Receive and parse message"""
        message = await self.websocket.recv()
        return json.loads(message)


async def demo():
    """Demonstration of WaddleAI MCP client"""
    # Configuration - replace with your actual API key
    MCP_URI = "ws://localhost:8765"
    API_KEY = "wa-admin-1-20241201"  # Replace with actual admin API key

    client = WaddleAIMCPClient(MCP_URI, API_KEY)

    try:
        # Connect to server
        if not await client.connect():
            return

        print("\n" + "=" * 50)
        print("WaddleAI MCP Client Demo")
        print("=" * 50)

        # List available tools
        print("\n1. Listing available tools:")
        tools = await client.list_tools()
        for tool in tools.get("tools", []):
            print(f"  • {tool['name']}: {tool['description']}")

        # List available resources
        print("\n2. Listing available resources:")
        resources = await client.list_resources()
        for resource in resources.get("resources", []):
            print(f"  • {resource['name']}: {resource['description']}")

        # Get available models
        print("\n3. Getting available models:")
        models_response = await client.call_tool("list_models", {})
        if "result" in models_response:
            models_content = json.loads(models_response["result"]["content"][0]["text"])
            models = models_content.get("models", [])
            print(f"  Found {len(models)} models:")
            for model in models[:5]:  # Show first 5
                print(f"    - {model['id']} ({model['provider']})")

        # Get usage statistics
        print("\n4. Getting usage statistics:")
        usage_response = await client.call_tool("get_usage", {"days": 7})
        if "result" in usage_response:
            usage_content = json.loads(usage_response["result"]["content"][0]["text"])
            print(f"  Last 7 days: {usage_content['total_tokens']} tokens, {usage_content['total_requests']} requests")

        # Test chat completion
        print("\n5. Testing chat completion:")
        messages = [{"role": "user", "content": "Hello! Can you tell me what WaddleAI is?"}]

        try:
            response = await client.chat(messages, model="gpt-3.5-turbo")
            print(f"  AI Response: {response[:200]}{'...' if len(response) > 200 else ''}")
        except Exception as e:
            print(f"  Chat failed: {e}")

        # Read usage analytics resource
        print("\n6. Reading usage analytics resource:")
        try:
            analytics = await client.read_resource("waddleai://usage/analytics")
            if "result" in analytics:
                content = json.loads(analytics["result"]["contents"][0]["text"])
                print(f"  Analytics period: {content['period']}")
                print(f"  Total tokens: {content['total_tokens']}")
                print(f"  Total requests: {content['total_requests']}")
        except Exception as e:
            print(f"  Failed to read analytics: {e}")

        print("\n" + "=" * 50)
        print("Demo completed successfully!")
        print("=" * 50)

    except Exception as e:
        print(f"Demo failed: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    print("WaddleAI MCP Client Example")
    print("Make sure the MCP server is running on ws://localhost:8765")
    print("Replace API_KEY in the demo() function with your actual API key")
    print()

    asyncio.run(demo())
