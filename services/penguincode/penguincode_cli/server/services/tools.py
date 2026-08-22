"""Tool callback service for client-side tool execution."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

import grpc

from penguincode_cli.proto import ToolCallbackServiceServicer, ToolRequest, ToolResponse

logger = logging.getLogger(__name__)


class PendingToolRequest:
    """Represents a pending tool request waiting for client response."""

    def __init__(self, request_id: str, session_id: str, tool_name: str, arguments: dict):
        self.request_id = request_id
        self.session_id = session_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.created_at = asyncio.get_event_loop().time()


class ToolCallbackServiceImpl(ToolCallbackServiceServicer):
    """Bidirectional streaming service for tool execution.

    The server sends ToolRequests to the client, and the client
    sends back ToolResponses with execution results.

    This enables tools like 'bash', 'read', 'write' to execute
    on the client side for security (filesystem access).
    """

    def __init__(self):
        # Pending requests by session_id
        self._pending_requests: dict[str, dict[str, PendingToolRequest]] = {}
        # Request queues by session_id (for streaming to clients)
        self._request_queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def register_session(self, session_id: str) -> asyncio.Queue:
        """Register a session for tool callbacks.

        Returns a queue that will receive ToolRequests.
        """
        async with self._lock:
            if session_id not in self._request_queues:
                self._request_queues[session_id] = asyncio.Queue()
                self._pending_requests[session_id] = {}
        return self._request_queues[session_id]

    async def unregister_session(self, session_id: str) -> None:
        """Unregister a session."""
        async with self._lock:
            self._request_queues.pop(session_id, None)
            # Cancel any pending requests
            pending = self._pending_requests.pop(session_id, {})
            for req in pending.values():
                if not req.future.done():
                    req.future.cancel()

    async def request_tool_execution(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict,
        timeout_seconds: int = 30,
    ) -> ToolResponse:
        """Request tool execution from the client.

        Called by ChatAgent when it needs to execute a tool.
        Blocks until client responds or timeout.
        """
        request_id = str(uuid.uuid4())

        # Create pending request
        pending = PendingToolRequest(
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        )

        async with self._lock:
            if session_id not in self._pending_requests:
                raise RuntimeError(f"Session {session_id} not registered for tool callbacks")
            self._pending_requests[session_id][request_id] = pending

            # Queue the request for the client
            queue = self._request_queues.get(session_id)
            if queue:
                await queue.put(
                    ToolRequest(
                        request_id=request_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        arguments={k: str(v) for k, v in arguments.items()},
                        timeout_seconds=timeout_seconds,
                    )
                )

        try:
            # Wait for response
            result = await asyncio.wait_for(pending.future, timeout=timeout_seconds)
            return result
        except TimeoutError:
            logger.warning(f"Tool request {request_id} timed out")
            return ToolResponse(
                request_id=request_id,
                success=False,
                error=f"Tool execution timed out after {timeout_seconds}s",
            )
        finally:
            async with self._lock:
                self._pending_requests.get(session_id, {}).pop(request_id, None)

    async def ExecuteTools(
        self,
        request_iterator: AsyncIterator[ToolResponse],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[ToolRequest]:
        """Bidirectional streaming for tool execution.

        Client calls this to establish a tool callback channel.
        Server sends ToolRequests, client sends ToolResponses.
        """
        # Extract session_id from metadata
        metadata = dict(context.invocation_metadata())
        session_id = metadata.get("session-id", "")

        if not session_id:
            logger.error("No session-id in tool callback metadata")
            return

        # Register session and get request queue
        queue = await self.register_session(session_id)
        logger.info(f"Tool callback channel established for session {session_id}")

        try:
            # Start a task to process incoming responses
            response_task = asyncio.create_task(self._process_responses(session_id, request_iterator))

            # Yield requests from the queue
            while True:
                try:
                    request = await asyncio.wait_for(queue.get(), timeout=60.0)
                    yield request
                except TimeoutError:
                    # Send keepalive or just continue
                    continue
                except asyncio.CancelledError:
                    break

        finally:
            response_task.cancel()
            await self.unregister_session(session_id)
            logger.info(f"Tool callback channel closed for session {session_id}")

    async def _process_responses(
        self,
        session_id: str,
        response_iterator: AsyncIterator[ToolResponse],
    ) -> None:
        """Process incoming tool responses from client."""
        try:
            async for response in response_iterator:
                async with self._lock:
                    pending = self._pending_requests.get(session_id, {}).get(response.request_id)

                if pending and not pending.future.done():
                    pending.future.set_result(response)
                    logger.debug(f"Received tool response for {response.request_id}")
                else:
                    logger.warning(f"Unexpected tool response: {response.request_id}")

        except Exception as e:
            logger.error(f"Error processing tool responses: {e}")
