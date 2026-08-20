"""Batched metering writer for token usage aggregation.

MeteringBuffer batches token usage events per-second at scale, aggregating
by (virtual_key, model, provider, minute-bucket) to reduce write load on the
database. The AIProxy is the sole writer to token_usage.

The buffer uses a pluggable writer seam to abstract the database layer,
allowing tests to substitute without coupling to penguin-dal or PyDAL.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

import tiktoken

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MeteringEvent:
    """A single token usage event from an LLM request.

    Attributes:
        virtual_key_id: ID of the virtual API key used
        model: Model name (e.g., "gpt-4")
        provider: Provider name (e.g., "openai")
        usage: Optional dict with input_tokens/output_tokens; None if provider
               did not report (triggers estimation at flush time)
        timestamp: When the event occurred
        estimated: True if usage was estimated (missing from provider response)
    """

    virtual_key_id: int
    model: str
    provider: str
    usage: dict[str, int] | None
    timestamp: datetime
    estimated: bool = False
    # Response cache accounting (spec §6.4). cache_status is one of
    # exact|semantic|upstream|miss; tokens_saved is 0 for misses.
    cache_status: str | None = None
    tokens_saved: int = 0


@dataclass(slots=True)
class AggregatedMetrics:
    """Aggregated metrics for a (vkey, model, provider, minute) bucket."""

    virtual_key_id: int
    model: str
    provider: str
    minute_bucket: datetime
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    request_count: int = 0
    has_estimated_usage: bool = False
    events: list = field(default_factory=list)
    # Response cache accounting (spec §6.4): summed tokens_saved across every
    # event in this bucket; cache_status is the bucket's last non-None
    # status (a bucket is 1 minute of one vkey/model/provider, so mixed
    # statuses within it are rare and last-write-wins is an acceptable
    # simplification for a dashboard-level aggregate).
    total_tokens_saved: int = 0
    cache_status: str | None = None


class UsageWriter(Protocol):
    """Protocol for writing aggregated usage metrics to the database.

    Implementations abstract away the DB layer (penguin-dal, PyDAL, etc).
    All writes must be blocking/synchronous; callers wrap in asyncio.to_thread.
    """

    def write_aggregated_row(self, agg: AggregatedMetrics) -> None:
        """Write aggregated metrics to token_usage table.

        Args:
            agg: Aggregated metrics to write or update

        Raises:
            Any exception from the database layer (caller handles)
        """
        ...


class PenguinDALUsageWriter:
    """Write aggregated metrics using penguin-dal.

    Thread-safe: penguin-dal manages its own thread-local connection pool.
    """

    def __init__(self, db: Any) -> None:
        """Initialize writer with penguin-dal DB instance.

        Args:
            db: penguin-dal DB instance (from penguin_dal.flask_ext.init_dal)
        """
        self.db = db
        self.source = "aiproxy"

    def write_aggregated_row(self, agg: AggregatedMetrics) -> None:
        """Write aggregated metrics to token_usage table.

        Creates new row or updates existing row for the (vkey, date) pair.
        Marks row as estimated=True if usage was missing from provider.

        Raises:
            Any exception from the database layer -- deliberately not caught
            here (see the ``UsageWriter`` protocol docstring above).
            ``MeteringBuffer.flush()`` treats a raised exception as a failed
            write and re-queues the aggregate for retry; a broad except in
            this method previously swallowed that signal (see regression
            note below), so a failed write was neither retried nor visible
            as anything worse than a log line.
        """
        # Check if row exists (using approximate match on date and key)
        # For simplicity, we'll upsert based on the day
        existing_row = (
            self.db(
                (self.db.token_usage.virtual_key_id == agg.virtual_key_id)
                & (self.db.token_usage.date == agg.minute_bucket.date())
            )
            .select()
            .first()
        )

        estimated_flag = 1 if agg.has_estimated_usage else 0

        if existing_row:
            # Update existing row
            new_input = existing_row.tokens_input_total + agg.total_input_tokens
            new_output = existing_row.tokens_output_total + agg.total_output_tokens
            new_requests = existing_row.request_count + agg.request_count

            # Merge LLM tokens breakdown (JSON)
            existing_breakdown = json.loads(existing_row.llm_tokens or "{}")
            model_key = f"{agg.provider}_{agg.model.replace('-', '_')}"
            if model_key not in existing_breakdown:
                existing_breakdown[model_key] = {"input": 0, "output": 0}
            existing_breakdown[model_key]["input"] += agg.total_input_tokens
            existing_breakdown[model_key]["output"] += agg.total_output_tokens

            new_tokens_saved = (existing_row.tokens_saved or 0) + agg.total_tokens_saved
            # regression: penguin_dal's Row (penguin_dal/query.py) has no
            # update_record() method (that's classic PyDAL API); the correct
            # penguin_dal update is db(condition).update(**kwargs) -- see the
            # identical fix in shared/auth/rbac.py and
            # shared/utils/token_manager.py. This previously raised
            # AttributeError on every *second* flush of the same
            # (virtual_key_id, day) pair, which was caught by this method's
            # own broad `except Exception` below and only logged -- so
            # flush()'s retry queue never saw the failure either, and the
            # aggregated tokens for that flush were dropped for good rather
            # than retried or counted. Real-world effect: only the first
            # metering flush of any given key/day ever landed; usage and
            # billing figures were silently undercounted after that.
            self.db(self.db.token_usage.id == existing_row.id).update(
                tokens_input_total=new_input,
                tokens_output_total=new_output,
                llm_tokens=json.dumps(existing_breakdown),
                request_count=new_requests,
                last_updated=datetime.utcnow(),
                source=self.source,
                estimated=existing_row.estimated or estimated_flag,
                tokens_saved=new_tokens_saved,
                cache_status=agg.cache_status or existing_row.cache_status,
            )
            logger.debug(
                "Updated token_usage row for vkey=%s model=%s requests=%s",
                agg.virtual_key_id,
                agg.model,
                agg.request_count,
            )
        else:
            # Create new row
            model_key = f"{agg.provider}_{agg.model.replace('-', '_')}"
            breakdown = {
                model_key: {"input": agg.total_input_tokens, "output": agg.total_output_tokens}
            }

            self.db.token_usage.insert(
                virtual_key_id=agg.virtual_key_id,
                user_id=None,  # Will be populated by management layer
                organization_id=None,  # Will be populated by management layer
                date=agg.minute_bucket.date(),
                waddleai_tokens=0,  # Calculated separately by cost system
                llm_tokens=json.dumps(breakdown),
                tokens_input_total=agg.total_input_tokens,
                tokens_output_total=agg.total_output_tokens,
                request_count=agg.request_count,
                cost_usd_total=0,  # Calculated separately by cost system
                source=self.source,
                estimated=estimated_flag,
                tokens_saved=agg.total_tokens_saved,
                cache_status=agg.cache_status,
            )
            logger.debug(
                "Inserted token_usage row for vkey=%s model=%s requests=%s",
                agg.virtual_key_id,
                agg.model,
                agg.request_count,
            )


class MeteringBuffer:
    """Batches token usage events and flushes aggregated metrics to the database.

    Aggregation key: (virtual_key_id, model, provider, minute_bucket)
    Flush interval: configurable (default 1.0 second)
    Writer: pluggable UsageWriter instance (abstracts DB layer)

    Missing usage handling:
      - When a provider doesn't report usage (common for streaming responses),
        estimate using tiktoken
      - Mark row as estimated=True so downstream can distinguish real from
        estimated usage

    Concurrency guarantees:
      - Buffer is swapped atomically under a lock during flush (no lost events)
      - Blocking DB writes wrapped in asyncio.to_thread (no event loop blocking)
      - Shutdown flushes remaining events
    """

    def __init__(self, writer: UsageWriter, interval: float = 1.0) -> None:
        """Initialize the metering buffer.

        Args:
            writer: UsageWriter instance for persisting aggregated metrics
            interval: Flush interval in seconds (default 1.0)
        """
        self.writer = writer
        self.interval = interval
        self._buffer: list[MeteringEvent] = []
        # Aggregates whose write failed; retried on the next flush so a database
        # blip does not silently discard billable usage. Bounded so a sustained
        # outage degrades to dropping the OLDEST rows (loudly) rather than
        # growing memory without limit.
        self._pending: list[AggregatedMetrics] = []
        self.max_pending_aggregates = 1000
        self._lock = Lock()
        self._running = False
        self._flush_task: asyncio.Task[Any] | None = None
        self._encoder = tiktoken.get_encoding("cl100k_base")  # Default OpenAI encoder

    def record(self, event: MeteringEvent) -> None:
        """Record a single token usage event.

        Thread-safe. Events are buffered in memory and flushed asynchronously.

        Args:
            event: MeteringEvent with virtual_key_id, model, provider, usage, timestamp
        """
        with self._lock:
            self._buffer.append(event)

    def start(self) -> None:
        """Start the background flush task."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._background_flush_loop())
        logger.info("MeteringBuffer background task started (interval=%fs)", self.interval)

    async def stop(self) -> None:
        """Stop the background flush task and flush any remaining buffered events.

        Should be called during shutdown to ensure no usage is lost.
        """
        self._running = False
        if self._flush_task:
            try:
                await asyncio.wait_for(self._flush_task, timeout=5.0)
            except TimeoutError:
                logger.warning("MeteringBuffer flush task did not complete within timeout")
                if self._flush_task:
                    self._flush_task.cancel()
        # Final flush of any remaining events
        await self.flush()
        logger.info("MeteringBuffer stopped")

    async def _background_flush_loop(self) -> None:
        """Background task that flushes buffered events periodically."""
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                await self.flush()
            except Exception as e:
                logger.error("Error in metering flush loop: %s", e, exc_info=True)

    async def flush(self) -> None:
        """Flush buffered events to the database.

        Aggregates events by (virtual_key_id, model, provider, minute_bucket),
        estimates missing usage, and writes/updates rows in token_usage table.

        Atomically swaps the buffer under a lock to prevent losing events
        recorded during the flush. All database writes are wrapped in
        asyncio.to_thread to avoid blocking the event loop.
        """
        # Swap the buffer under lock (prevent lost updates)
        with self._lock:
            current_buffer = self._buffer[:]
            self._buffer.clear()
            retries = self._pending[:]
            self._pending.clear()

        if not current_buffer and not retries:
            return

        # Aggregate outside the lock; retries from a previous failed flush go first.
        aggregates = self._aggregate_events(current_buffer) if current_buffer else {}
        to_write = retries + list(aggregates.values())

        # Write aggregated data to database (blocking calls in thread pool).
        # A failed row is re-queued rather than dropped — losing it would mean
        # unbilled tokens with no error surface anywhere.
        failed: list[AggregatedMetrics] = []
        for agg in to_write:
            try:
                await asyncio.to_thread(self.writer.write_aggregated_row, agg)
            except Exception as e:
                logger.error(
                    "Metering write failed for vkey=%s model=%s; re-queueing (%s)",
                    agg.virtual_key_id,
                    agg.model,
                    e,
                )
                failed.append(agg)

        if failed:
            with self._lock:
                self._pending.extend(failed)
                overflow = len(self._pending) - self.max_pending_aggregates
                if overflow > 0:
                    dropped = self._pending[:overflow]
                    del self._pending[:overflow]
                    logger.error(
                        "Metering retry queue full (%d): dropping %d aggregate(s), "
                        "%d input + %d output tokens will be unbilled",
                        self.max_pending_aggregates,
                        overflow,
                        sum(a.total_input_tokens for a in dropped),
                        sum(a.total_output_tokens for a in dropped),
                    )

    def _aggregate_events(self, events: list[MeteringEvent]) -> dict[tuple, AggregatedMetrics]:
        """Aggregate events by (virtual_key_id, model, provider, minute_bucket).

        Estimates missing usage using tiktoken.

        Returns:
            Dict mapping aggregation key to AggregatedMetrics
        """
        aggregates: dict[tuple, AggregatedMetrics] = {}

        for event in events:
            key = self._get_aggregation_key(event)

            if key not in aggregates:
                aggregates[key] = AggregatedMetrics(
                    virtual_key_id=event.virtual_key_id,
                    model=event.model,
                    provider=event.provider,
                    minute_bucket=key[3],
                )

            agg = aggregates[key]

            # Use provided usage or estimate
            if event.usage:
                input_tokens = event.usage.get("input_tokens", 0)
                output_tokens = event.usage.get("output_tokens", 0)
            else:
                # Estimate missing usage
                estimated = self._estimate_tokens(event)
                input_tokens = estimated.get("input_tokens", 0)
                output_tokens = estimated.get("output_tokens", 0)
                agg.has_estimated_usage = True

            agg.total_input_tokens += input_tokens
            agg.total_output_tokens += output_tokens
            agg.request_count += 1
            agg.total_tokens_saved += event.tokens_saved or 0
            if event.cache_status is not None:
                agg.cache_status = event.cache_status
            agg.events.append(event)

        return aggregates

    def _get_aggregation_key(self, event: MeteringEvent) -> tuple:
        """Get aggregation key: (vkey, model, provider, minute_bucket).

        Rounds timestamp to minute precision (second/microsecond = 0).
        """
        minute_bucket = event.timestamp.replace(second=0, microsecond=0)
        return (event.virtual_key_id, event.model, event.provider, minute_bucket)

    def _estimate_tokens(self, event: MeteringEvent) -> dict[str, int]:
        """Estimate token counts using tiktoken (fallback for missing usage).

        Returns:
            Dict with 'input_tokens' and 'output_tokens' keys
        """
        # For events without usage, estimate conservatively
        # In a real scenario, we'd have prompt/response text to count
        # For now, use a minimal estimate (1 token each as placeholder)
        # This ensures we don't lose events with zero counts
        return {
            "input_tokens": 1,
            "output_tokens": 1,
        }


def create_metering_buffer(
    writer: UsageWriter | None = None, db: Any | None = None, interval: float = 1.0
) -> MeteringBuffer:
    """Factory function to create a MeteringBuffer instance.

    Supports two modes:
      - If writer is provided, use it directly (for testing/custom implementations)
      - If db is provided, create a PenguinDALUsageWriter (for production)

    Args:
        writer: Optional UsageWriter instance (takes precedence)
        db: Optional penguin-dal DB instance (used if writer is None)
        interval: Flush interval in seconds

    Returns:
        Initialized MeteringBuffer instance

    Raises:
        ValueError: If neither writer nor db is provided
    """
    if writer is None:
        if db is None:
            raise ValueError("Must provide either writer or db parameter")
        writer = PenguinDALUsageWriter(db)

    return MeteringBuffer(writer, interval=interval)
