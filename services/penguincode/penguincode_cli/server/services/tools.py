"""Tool callback service for client-side tool execution."""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator

import grpc

from penguincode_cli.flags.client import DISABLE_TOOL_QUEUE_BOUND_FLAG, SYSTEM_SCOPE, is_enabled
from penguincode_cli.observability.otel import record_tool_queue_event
from penguincode_cli.proto import ToolCallbackServiceServicer, ToolRequest, ToolResponse

logger = logging.getLogger(__name__)

#: O10 (gRPC server hardening): default bound on each session's pending
#: tool-request queue -- was previously `asyncio.Queue()` with no `maxsize`
#: at all, letting a slow/stalled client's queue grow without limit.
_DEFAULT_TOOL_QUEUE_MAXSIZE = 256


def _tool_queue_maxsize() -> int:
    """Resolve the per-session tool queue bound from `PENGUINCODE_TOOL_QUEUE_MAXSIZE`.

    Falls back to `_DEFAULT_TOOL_QUEUE_MAXSIZE` on unset, blank, non-numeric,
    or non-positive values -- a malformed tunable must never crash the
    server or silently produce an unbounded queue.
    """
    raw = os.environ.get("PENGUINCODE_TOOL_QUEUE_MAXSIZE")
    if raw is None or not raw.strip():
        return _DEFAULT_TOOL_QUEUE_MAXSIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid PENGUINCODE_TOOL_QUEUE_MAXSIZE=%r; using default %d",
            raw,
            _DEFAULT_TOOL_QUEUE_MAXSIZE,
        )
        return _DEFAULT_TOOL_QUEUE_MAXSIZE
    if value <= 0:
        logger.warning(
            "PENGUINCODE_TOOL_QUEUE_MAXSIZE must be positive, got %d; using default %d",
            value,
            _DEFAULT_TOOL_QUEUE_MAXSIZE,
        )
        return _DEFAULT_TOOL_QUEUE_MAXSIZE
    return value


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

        Returns a queue that will receive ToolRequests. Bounded at
        `_tool_queue_maxsize()` (O10, gRPC server hardening) unless the
        `waddleai.disable-tool-queue-bound` kill-switch reverts to the
        pre-hardening unbounded queue (`maxsize=0`).
        """
        async with self._lock:
            if session_id not in self._request_queues:
                if is_enabled(DISABLE_TOOL_QUEUE_BOUND_FLAG, SYSTEM_SCOPE):
                    logger.warning(
                        "Tool-callback queue bound disabled via kill-switch (%s) for "
                        "session %s; queue is unbounded",
                        DISABLE_TOOL_QUEUE_BOUND_FLAG,
                        session_id,
                    )
                    maxsize = 0
                else:
                    maxsize = _tool_queue_maxsize()
                self._request_queues[session_id] = asyncio.Queue(maxsize=maxsize)
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

        queue_full = False

        async with self._lock:
            if session_id not in self._pending_requests:
                raise RuntimeError(f"Session {session_id} not registered for tool callbacks")
            self._pending_requests[session_id][request_id] = pending

            # Queue the request for the client. `put_nowait` (not `put`) --
            # O10, gRPC server hardening: a bounded queue must reject
            # immediately on full rather than block indefinitely, which
            # would hang this call well past `timeout_seconds` (the
            # `wait_for` below never even starts).
            queue = self._request_queues.get(session_id)
            if queue is not None:
                try:
                    queue.put_nowait(
                        ToolRequest(
                            request_id=request_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            arguments={k: str(v) for k, v in arguments.items()},
                            timeout_seconds=timeout_seconds,
                        )
                    )
                    record_tool_queue_event("enqueued")
                except asyncio.QueueFull:
                    queue_full = True

            if queue_full:
                self._pending_requests[session_id].pop(request_id, None)

        if queue_full:
            logger.warning(
                "Tool request queue full for session %s (maxsize=%d); rejecting tool=%s",
                session_id,
                queue.maxsize if queue is not None else -1,
                tool_name,
            )
            record_tool_queue_event("rejected")
            return ToolResponse(
                request_id=request_id,
                success=False,
                error=(
                    f"Tool request queue full (max {queue.maxsize if queue is not None else '?'} "
                    "pending); try again shortly"
                ),
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
            response_task = asyncio.create_task(
                self._process_responses(session_id, request_iterator)
            )

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
