"""Tests for usage module - UsageTracker, UsageRecord."""

import json

import pytest

pytest.importorskip("penguincode_cli.usage")

from penguincode_cli.usage.tracker import UsageRecord, UsageTracker


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create temporary project directory."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def usage_tracker(temp_project_dir):
    """Create UsageTracker instance."""
    return UsageTracker(temp_project_dir)


def test_usage_record_initialization():
    """Test UsageRecord dataclass initialization."""
    record = UsageRecord(
        timestamp="2024-01-01T00:00:00",
        model="test-model:latest",
        input_tokens=100,
        output_tokens=50,
        duration_ms=150.5,
        agent_name="test_agent",
    )
    assert record.model == "test-model:latest"
    assert record.input_tokens == 100
    assert record.output_tokens == 50
    assert record.duration_ms == 150.5
    assert record.agent_name == "test_agent"


def test_usage_tracker_initialization(usage_tracker, temp_project_dir):
    """Test UsageTracker initialization."""
    assert usage_tracker.project_dir == temp_project_dir
    assert usage_tracker.usage_dir == temp_project_dir / ".penguincode"
    assert usage_tracker.usage_file == temp_project_dir / ".penguincode" / "usage.json"
    assert usage_tracker.session_records == []
    assert usage_tracker.usage_dir.exists()


def test_usage_tracker_record(usage_tracker):
    """Test recording usage."""
    usage_tracker.record(
        model="test-model:latest",
        input_tokens=100,
        output_tokens=50,
        duration_ms=150.5,
        agent="test_agent",
    )

    assert len(usage_tracker.session_records) == 1
    record = usage_tracker.session_records[0]
    assert record.model == "test-model:latest"
    assert record.input_tokens == 100
    assert record.output_tokens == 50


def test_usage_tracker_persist_record(usage_tracker):
    """Test persisting records to file."""
    usage_tracker.record(
        model="test-model:latest",
        input_tokens=100,
        output_tokens=50,
        duration_ms=150.5,
    )

    assert usage_tracker.usage_file.exists()

    with open(usage_tracker.usage_file) as f:
        records = json.load(f)

    assert len(records) == 1
    assert records[0]["model"] == "test-model:latest"


def test_usage_tracker_get_session_usage_empty(usage_tracker):
    """Test session usage with no records."""
    usage = usage_tracker.get_session_usage()
    assert usage["total_input_tokens"] == 0
    assert usage["total_output_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["request_count"] == 0


def test_usage_tracker_get_session_usage(usage_tracker):
    """Test session usage calculation."""
    usage_tracker.record("model1", 100, 50, 100.0, "agent1")
    usage_tracker.record("model1", 200, 100, 200.0, "agent1")

    usage = usage_tracker.get_session_usage()
    assert usage["total_input_tokens"] == 300
    assert usage["total_output_tokens"] == 150
    assert usage["total_tokens"] == 450
    assert usage["total_duration_ms"] == 300.0
    assert usage["request_count"] == 2
    assert usage["tokens_per_second"] > 0


def test_usage_tracker_get_project_usage_empty(usage_tracker):
    """Test project usage with no file."""
    usage = usage_tracker.get_project_usage()
    assert usage["total_tokens"] == 0
    assert usage["request_count"] == 0


def test_usage_tracker_get_project_usage(usage_tracker):
    """Test project usage from persisted file."""
    usage_tracker.record("model1", 100, 50, 100.0)
    usage_tracker.record("model1", 200, 100, 200.0)

    usage = usage_tracker.get_project_usage()
    assert usage["total_input_tokens"] == 300
    assert usage["total_output_tokens"] == 150
    assert usage["total_tokens"] == 450
    assert usage["request_count"] == 2


def test_usage_tracker_get_usage_by_model(usage_tracker):
    """Test usage breakdown by model."""
    usage_tracker.record("model1", 100, 50, 100.0)
    usage_tracker.record("model2", 200, 100, 200.0)
    usage_tracker.record("model1", 150, 75, 150.0)

    by_model = usage_tracker.get_usage_by_model()

    assert "model1" in by_model
    assert "model2" in by_model
    assert by_model["model1"]["total_input_tokens"] == 250
    assert by_model["model1"]["request_count"] == 2
    assert by_model["model2"]["total_input_tokens"] == 200
    assert by_model["model2"]["request_count"] == 1


def test_usage_tracker_get_usage_by_agent(usage_tracker):
    """Test usage breakdown by agent."""
    usage_tracker.record("model1", 100, 50, 100.0, "agent1")
    usage_tracker.record("model1", 200, 100, 200.0, "agent2")
    usage_tracker.record("model1", 150, 75, 150.0, "agent1")

    by_agent = usage_tracker.get_usage_by_agent()

    assert "agent1" in by_agent
    assert "agent2" in by_agent
    assert by_agent["agent1"]["total_input_tokens"] == 250
    assert by_agent["agent1"]["request_count"] == 2
    assert by_agent["agent2"]["total_input_tokens"] == 200


def test_usage_tracker_get_usage_by_agent_unknown(usage_tracker):
    """Test usage tracking for agent=None."""
    usage_tracker.record("model1", 100, 50, 100.0, agent=None)

    by_agent = usage_tracker.get_usage_by_agent()
    assert "unknown" in by_agent
    assert by_agent["unknown"]["total_input_tokens"] == 100


def test_usage_tracker_tokens_per_second_calculation(usage_tracker):
    """Test tokens per second calculation."""
    usage_tracker.record("model1", 1000, 500, 1000.0)  # 1 second, 1500 tokens

    usage = usage_tracker.get_session_usage()
    assert usage["tokens_per_second"] == 1500.0


def test_usage_tracker_multiple_records_accumulation(usage_tracker):
    """Test that multiple records accumulate properly."""
    for _i in range(10):
        usage_tracker.record("model1", 10, 5, 10.0, "agent1")

    usage = usage_tracker.get_session_usage()
    assert usage["total_input_tokens"] == 100
    assert usage["total_output_tokens"] == 50
    assert usage["request_count"] == 10

    # Verify persistence
    with open(usage_tracker.usage_file) as f:
        records = json.load(f)
    assert len(records) == 10


def test_usage_tracker_zero_duration_handling(usage_tracker):
    """Test handling of zero duration for tokens_per_second."""
    usage_tracker.record("model1", 100, 50, 0.0)

    usage = usage_tracker.get_session_usage()
    assert usage["tokens_per_second"] == 0
