"""
Unit tests for MeteringBuffer - batched token usage aggregation
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from shared.utils.metering import (
    AggregatedMetrics,
    MeteringBuffer,
    MeteringEvent,
    UsageWriter,
    create_metering_buffer,
)


class MockUsageWriter(UsageWriter):
    """Mock writer for testing"""

    def __init__(self):
        """Initialize with tracking lists"""
        self.writes: list[AggregatedMetrics] = []
        self.write_count = 0

    def write_aggregated_row(self, agg: AggregatedMetrics) -> None:
        """Record a write call"""
        self.writes.append(agg)
        self.write_count += 1


class TestMeteringEvent:
    """Test MeteringEvent dataclass"""

    def test_event_with_usage(self):
        """Test event creation with usage"""
        usage = {"input_tokens": 100, "output_tokens": 50}
        event = MeteringEvent(
            virtual_key_id=123,
            model="gpt-4",
            provider="openai",
            usage=usage,
            timestamp=datetime.utcnow(),
        )
        assert event.virtual_key_id == 123
        assert event.model == "gpt-4"
        assert event.usage["input_tokens"] == 100
        assert event.estimated is False

    def test_event_without_usage(self):
        """Test event creation without usage (will require estimation)"""
        event = MeteringEvent(
            virtual_key_id=123,
            model="claude-3-opus",
            provider="anthropic",
            usage=None,
            timestamp=datetime.utcnow(),
        )
        assert event.usage is None
        assert event.estimated is False  # Initially false; marked true after estimation


class TestMeteringBuffer:
    """Test MeteringBuffer batching and aggregation"""

    @pytest.fixture
    def mock_writer(self):
        """Create mock writer"""
        return MockUsageWriter()

    def test_buffer_initialization(self, mock_writer):
        """Test buffer initialization"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        assert buffer.writer is mock_writer
        assert buffer.interval == 1.0
        assert len(buffer._buffer) == 0

    def test_record_single_event(self, mock_writer):
        """Test recording a single event"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        usage = {"input_tokens": 100, "output_tokens": 50}
        event = MeteringEvent(
            virtual_key_id=1,
            model="gpt-4",
            provider="openai",
            usage=usage,
            timestamp=datetime.utcnow(),
        )
        buffer.record(event)
        assert len(buffer._buffer) == 1

    def test_record_multiple_events_same_key(self, mock_writer):
        """Test recording multiple events for the same key"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        now = datetime.utcnow()
        for i in range(3):
            usage = {"input_tokens": 100 + i, "output_tokens": 50}
            event = MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage=usage,
                timestamp=now,
            )
            buffer.record(event)
        assert len(buffer._buffer) == 3

    def test_aggregation_key_format(self, mock_writer):
        """Test aggregation key includes minute bucket"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        now = datetime(2026, 7, 28, 12, 34, 45)
        event = MeteringEvent(
            virtual_key_id=1,
            model="gpt-4",
            provider="openai",
            usage={"input_tokens": 100, "output_tokens": 50},
            timestamp=now,
        )
        # Key should include minute bucket (vkey, model, provider, minute)
        key = buffer._get_aggregation_key(event)
        assert key[0] == 1  # virtual_key_id
        assert key[1] == "gpt-4"
        assert key[2] == "openai"
        # key[3] should be a minute-level bucket (datetime rounded to minute)
        assert isinstance(key[3], datetime)
        # Verify it's rounded to minute precision (second/microsecond = 0)
        assert key[3].second == 0
        assert key[3].microsecond == 0

    def test_aggregation_same_minute_coalescses(self, mock_writer):
        """Test that events in same minute coalesce"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        base_time = datetime(2026, 7, 28, 12, 34, 0)

        events_data = [
            (1, "gpt-4", "openai", 100, 50),
            (1, "gpt-4", "openai", 200, 75),  # Same key, same minute
            (1, "gpt-4", "openai", 50, 25),   # Same key, same minute
        ]

        for vkey, model, provider, inp, out in events_data:
            event = MeteringEvent(
                virtual_key_id=vkey,
                model=model,
                provider=provider,
                usage={"input_tokens": inp, "output_tokens": out},
                timestamp=base_time + timedelta(seconds=30),  # All in same minute
            )
            buffer.record(event)

        # Verify events are buffered (not yet flushed)
        assert len(buffer._buffer) == 3

    def test_missing_usage_fallback(self, mock_writer):
        """Test fallback token estimation for missing usage"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        event = MeteringEvent(
            virtual_key_id=1,
            model="gpt-4",
            provider="openai",
            usage=None,  # Missing usage
            timestamp=datetime.utcnow(),
        )
        buffer.record(event)
        # The event is buffered; estimation happens at flush time
        assert len(buffer._buffer) == 1

    @pytest.mark.asyncio
    async def test_flush_writes_aggregated_rows(self, mock_writer):
        """Test that flush writes aggregated rows to database"""
        buffer = MeteringBuffer(mock_writer, interval=10.0)  # Long interval
        now = datetime.utcnow()

        # Record events
        for i in range(3):
            event = MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage={"input_tokens": 100 + i, "output_tokens": 50},
                timestamp=now,
            )
            buffer.record(event)

        # Manually flush (don't start background task)
        await buffer.flush()

        # Verify only 1 write occurred (aggregated, not 3 separate writes)
        assert mock_writer.write_count == 1

    @pytest.mark.asyncio
    async def test_flush_aggregates_counts(self, mock_writer):
        """Test that flush properly aggregates token counts"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        now = datetime.utcnow()

        total_input = 0
        total_output = 0
        event_count = 5

        for i in range(event_count):
            inp = 100 + i
            out = 50 + i
            total_input += inp
            total_output += out
            event = MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage={"input_tokens": inp, "output_tokens": out},
                timestamp=now,
            )
            buffer.record(event)

        await buffer.flush()

        # Verify aggregated totals
        assert len(mock_writer.writes) == 1
        agg = mock_writer.writes[0]
        assert agg.total_input_tokens == total_input
        assert agg.total_output_tokens == total_output
        assert agg.request_count == event_count

    @pytest.mark.asyncio
    async def test_concurrent_record_during_flush(self, mock_writer):
        """Test that events recorded during flush are not lost"""
        # Mock the writer to be slow
        slow_writer = MockUsageWriter()

        async def slow_write(agg):
            await asyncio.sleep(0.1)
            slow_writer.write_aggregated_row(agg)

        buffer = MeteringBuffer(slow_writer, interval=1.0)
        now = datetime.utcnow()

        # Record initial events
        for i in range(2):
            event = MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage={"input_tokens": 100, "output_tokens": 50},
                timestamp=now,
            )
            buffer.record(event)

        # Patch writer to be async slow
        with patch.object(slow_writer, "write_aggregated_row", side_effect=slow_write):
            # Start flush task
            flush_task = asyncio.create_task(buffer.flush())

            # Give flush time to start swapping
            await asyncio.sleep(0.01)

            # Record more events during flush (after swap but before write complete)
            for i in range(3):
                event = MeteringEvent(
                    virtual_key_id=1,
                    model="gpt-4",
                    provider="openai",
                    usage={"input_tokens": 100, "output_tokens": 50},
                    timestamp=now,
                )
                buffer.record(event)

            # Wait for flush to complete
            await flush_task

            # Events recorded after swap should still be in buffer
            assert len(buffer._buffer) == 3

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining_events(self, mock_writer):
        """Test that stop() flushes remaining buffered events"""
        buffer = MeteringBuffer(mock_writer, interval=10.0)  # Long interval
        now = datetime.utcnow()

        # Record some events
        for i in range(3):
            event = MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage={"input_tokens": 100, "output_tokens": 50},
                timestamp=now,
            )
            buffer.record(event)

        assert len(buffer._buffer) == 3

        # Stop should flush remaining events
        await buffer.stop()

        # Buffer should be empty after stop
        assert len(buffer._buffer) == 0
        # And should have written 1 aggregated row
        assert mock_writer.write_count == 1

    @pytest.mark.asyncio
    async def test_background_flush_task(self, mock_writer):
        """Test that background flush task runs"""
        buffer = MeteringBuffer(mock_writer, interval=0.1)  # Short interval
        now = datetime.utcnow()

        # Record an event
        event = MeteringEvent(
            virtual_key_id=1,
            model="gpt-4",
            provider="openai",
            usage={"input_tokens": 100, "output_tokens": 50},
            timestamp=now,
        )
        buffer.record(event)

        # Start background task
        buffer.start()

        # Wait for at least one flush cycle
        await asyncio.sleep(0.2)

        # Stop the background task
        await buffer.stop()

        # Buffer should be empty (flushed by background task)
        assert len(buffer._buffer) == 0
        # And should have at least one write
        assert mock_writer.write_count >= 1

    def test_estimated_usage_fallback_calculation(self, mock_writer):
        """Test that estimated usage is calculated correctly"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)

        # Create an event without usage
        event = MeteringEvent(
            virtual_key_id=1,
            model="gpt-4",
            provider="openai",
            usage=None,
            timestamp=datetime.utcnow(),
        )

        # Estimate should be calculated using tiktoken
        estimated = buffer._estimate_tokens(event)

        # Should return a dict with input and output tokens
        assert isinstance(estimated, dict)
        assert "input_tokens" in estimated
        assert "output_tokens" in estimated
        assert estimated["input_tokens"] >= 0
        assert estimated["output_tokens"] >= 0

    @pytest.mark.asyncio
    async def test_missing_usage_marked_as_estimated(self, mock_writer):
        """Test that rows with missing usage are marked as estimated"""
        buffer = MeteringBuffer(mock_writer, interval=1.0)
        now = datetime.utcnow()

        # Record event with missing usage
        event = MeteringEvent(
            virtual_key_id=1,
            model="claude-3-opus",
            provider="anthropic",
            usage=None,
            timestamp=now,
        )
        buffer.record(event)

        await buffer.flush()

        # Verify that the aggregated row is marked as estimated
        assert len(mock_writer.writes) == 1
        agg = mock_writer.writes[0]
        assert agg.has_estimated_usage is True


class TestCreateMeteringBuffer:
    """Test factory function"""

    def test_create_metering_buffer_with_writer(self):
        """Test create_metering_buffer factory with writer"""
        mock_writer = MockUsageWriter()
        buffer = create_metering_buffer(writer=mock_writer, interval=1.0)
        assert isinstance(buffer, MeteringBuffer)
        assert buffer.writer is mock_writer
        assert buffer.interval == 1.0

    def test_create_metering_buffer_without_writer_or_db_raises(self):
        """Test create_metering_buffer raises when neither writer nor db provided"""
        with pytest.raises(ValueError, match="Must provide either writer or db"):
            create_metering_buffer()


class TestFlushFailureDoesNotLoseUsage:
    """A failed DB write must not silently discard billable usage.

    flush() clears the buffer before writing. Without retry handling, any
    writer exception (DB restart, connection blip) permanently drops every
    event in that window — unbilled tokens and an effective quota bypass.
    """

    @pytest.mark.asyncio
    async def test_failed_write_is_retried_on_next_flush(self):
        writer = MagicMock()
        writer.write_aggregated_row.side_effect = [RuntimeError("db down"), None]

        buf = MeteringBuffer(writer=writer, interval=0.01)
        buf.record(
            MeteringEvent(
                virtual_key_id=1,
                model="gpt-4",
                provider="openai",
                usage={"input_tokens": 10, "output_tokens": 5},
                timestamp=datetime.utcnow(),
            )
        )

        await buf.flush()  # write raises -> must not drop the usage
        await buf.flush()  # retry succeeds

        assert writer.write_aggregated_row.call_count == 2
        retried = writer.write_aggregated_row.call_args_list[1][0][0]
        assert retried.total_input_tokens == 10
        assert retried.total_output_tokens == 5

    @pytest.mark.asyncio
    async def test_pending_retries_are_bounded(self):
        """A permanently failing writer must not grow memory without limit."""
        writer = MagicMock()
        writer.write_aggregated_row.side_effect = RuntimeError("db down")

        buf = MeteringBuffer(writer=writer, interval=0.01)
        buf.max_pending_aggregates = 5  # keep the bound test fast
        for i in range(buf.max_pending_aggregates + 25):
            buf.record(
                MeteringEvent(
                    virtual_key_id=i,  # distinct key -> distinct aggregate
                    model="gpt-4",
                    provider="openai",
                    usage={"input_tokens": 1, "output_tokens": 1},
                    timestamp=datetime.utcnow(),
                )
            )
            await buf.flush()

        assert len(buf._pending) <= buf.max_pending_aggregates
