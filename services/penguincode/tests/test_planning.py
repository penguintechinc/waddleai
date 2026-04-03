"""Tests for planning module - Plan, TodoItem, PlanTracker."""

import pytest

pytest.importorskip("penguincode_cli.planning")

from penguincode_cli.planning.plan import Plan, PlanStep, TodoItem, TodoStatus
from penguincode_cli.planning.tracker import PlanTracker


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create temporary project directory."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def plan_tracker(temp_project_dir):
    """Create PlanTracker instance."""
    return PlanTracker(temp_project_dir)


def test_todo_status_enum():
    """Test TodoStatus enum values."""
    assert TodoStatus.PENDING.value == "pending"
    assert TodoStatus.IN_PROGRESS.value == "in_progress"
    assert TodoStatus.COMPLETED.value == "completed"


def test_todo_item_initialization():
    """Test TodoItem initialization."""
    todo = TodoItem(content="Test task")
    assert todo.content == "Test task"
    assert todo.status == TodoStatus.PENDING
    assert todo.completed_at is None
    assert todo.id is not None


def test_todo_item_to_dict():
    """Test TodoItem to_dict method."""
    todo = TodoItem(content="Test task", status=TodoStatus.COMPLETED)
    todo_dict = todo.to_dict()
    assert todo_dict["content"] == "Test task"
    assert todo_dict["status"] == "completed"
    assert "id" in todo_dict
    assert "created_at" in todo_dict


def test_todo_item_from_dict():
    """Test TodoItem from_dict factory method."""
    data = {
        "id": "test-id-123",
        "content": "Test task",
        "status": "in_progress",
        "created_at": "2024-01-01T00:00:00",
        "completed_at": None,
    }
    todo = TodoItem.from_dict(data)
    assert todo.id == "test-id-123"
    assert todo.content == "Test task"
    assert todo.status == TodoStatus.IN_PROGRESS


def test_plan_step_initialization():
    """Test PlanStep initialization."""
    step = PlanStep(description="First step")
    assert step.description == "First step"
    assert step.completed is False
    assert step.substeps == []


def test_plan_step_with_substeps():
    """Test PlanStep with substeps."""
    substep = PlanStep(description="Substep 1")
    step = PlanStep(description="Main step", substeps=[substep])
    assert len(step.substeps) == 1
    assert step.substeps[0].description == "Substep 1"


def test_plan_step_to_dict():
    """Test PlanStep to_dict method."""
    step = PlanStep(description="Test step", completed=True)
    step_dict = step.to_dict()
    assert step_dict["description"] == "Test step"
    assert step_dict["completed"] is True
    assert "id" in step_dict


def test_plan_initialization():
    """Test Plan initialization."""
    plan = Plan(name="Test Plan", description="A test plan")
    assert plan.name == "Test Plan"
    assert plan.description == "A test plan"
    assert plan.steps == []
    assert plan.todos == []


def test_plan_add_todo():
    """Test adding todo to plan."""
    plan = Plan(name="Test Plan")
    todo = plan.add_todo("New task")
    assert len(plan.todos) == 1
    assert plan.todos[0] == todo
    assert todo.content == "New task"


def test_plan_complete_todo():
    """Test completing a todo."""
    plan = Plan(name="Test Plan")
    todo = plan.add_todo("Task to complete")
    success = plan.complete_todo(todo.id)
    assert success is True
    assert todo.status == TodoStatus.COMPLETED
    assert todo.completed_at is not None


def test_plan_complete_nonexistent_todo():
    """Test completing non-existent todo returns False."""
    plan = Plan(name="Test Plan")
    success = plan.complete_todo("nonexistent-id")
    assert success is False


def test_plan_add_step():
    """Test adding step to plan."""
    plan = Plan(name="Test Plan")
    step = plan.add_step("First step")
    assert len(plan.steps) == 1
    assert step.description == "First step"


def test_plan_get_progress_empty():
    """Test progress calculation for empty plan."""
    plan = Plan(name="Empty Plan")
    progress = plan.get_progress()
    assert progress == 0.0


def test_plan_get_progress_with_steps():
    """Test progress calculation with steps."""
    plan = Plan(name="Test Plan")
    step1 = plan.add_step("Step 1")
    step2 = plan.add_step("Step 2")

    # No steps completed
    assert plan.get_progress() == 0.0

    # One step completed
    step1.completed = True
    assert plan.get_progress() == 0.5

    # All steps completed
    step2.completed = True
    assert plan.get_progress() == 1.0


def test_plan_get_progress_with_todos():
    """Test progress calculation with todos."""
    plan = Plan(name="Test Plan")
    plan.add_todo("Task 1")
    plan.add_todo("Task 2")

    # Complete one todo
    plan.complete_todo(plan.todos[0].id)
    assert plan.get_progress() == 0.5


def test_plan_to_dict():
    """Test Plan to_dict method."""
    plan = Plan(name="Test Plan", description="Test description")
    plan.add_step("Step 1")
    plan.add_todo("Task 1")

    plan_dict = plan.to_dict()
    assert plan_dict["name"] == "Test Plan"
    assert plan_dict["description"] == "Test description"
    assert len(plan_dict["steps"]) == 1
    assert len(plan_dict["todos"]) == 1


def test_plan_from_dict():
    """Test Plan from_dict factory method."""
    data = {
        "name": "Test Plan",
        "description": "Test description",
        "steps": [{"id": "s1", "description": "Step 1", "substeps": [], "completed": False}],
        "todos": [{"id": "t1", "content": "Task 1", "status": "pending", "created_at": "2024-01-01T00:00:00", "completed_at": None}],
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }
    plan = Plan.from_dict(data)
    assert plan.name == "Test Plan"
    assert len(plan.steps) == 1
    assert len(plan.todos) == 1


@pytest.mark.asyncio
async def test_plan_tracker_save_plan(plan_tracker, temp_project_dir):
    """Test saving plan to file."""
    plan = Plan(name="Test Plan", description="Save test")
    plan.add_step("Step 1")

    await plan_tracker.save_plan(plan)

    plan_file = temp_project_dir / ".PLAN"
    assert plan_file.exists()


@pytest.mark.asyncio
async def test_plan_tracker_load_plan(plan_tracker, temp_project_dir):
    """Test loading plan from file."""
    plan = Plan(name="Test Plan", description="Load test")
    plan.add_step("Step 1")
    await plan_tracker.save_plan(plan)

    loaded_plan = await plan_tracker.load_plan()
    assert loaded_plan is not None
    assert loaded_plan.name == "Test Plan"
    assert len(loaded_plan.steps) == 1


@pytest.mark.asyncio
async def test_plan_tracker_load_nonexistent_plan(plan_tracker):
    """Test loading non-existent plan returns None."""
    loaded_plan = await plan_tracker.load_plan()
    assert loaded_plan is None


@pytest.mark.asyncio
async def test_plan_tracker_save_todos(plan_tracker, temp_project_dir):
    """Test saving todos to file."""
    todos = [
        TodoItem(content="Task 1"),
        TodoItem(content="Task 2", status=TodoStatus.COMPLETED),
    ]

    await plan_tracker.save_todos(todos)

    todo_file = temp_project_dir / ".TODO"
    assert todo_file.exists()


@pytest.mark.asyncio
async def test_plan_tracker_load_todos(plan_tracker):
    """Test loading todos from file."""
    todos = [TodoItem(content="Task 1"), TodoItem(content="Task 2")]
    await plan_tracker.save_todos(todos)

    loaded_todos = await plan_tracker.load_todos()
    assert len(loaded_todos) == 2
    assert loaded_todos[0].content == "Task 1"


@pytest.mark.asyncio
async def test_plan_tracker_load_nonexistent_todos(plan_tracker):
    """Test loading non-existent todos returns empty list."""
    loaded_todos = await plan_tracker.load_todos()
    assert loaded_todos == []


@pytest.mark.asyncio
async def test_plan_tracker_auto_save(plan_tracker):
    """Test auto_save method."""
    plan = Plan(name="Auto Save Test")
    todos = [TodoItem(content="Auto Task")]

    await plan_tracker.auto_save(plan=plan, todos=todos)

    loaded_plan = await plan_tracker.load_plan()
    loaded_todos = await plan_tracker.load_todos()

    assert loaded_plan.name == "Auto Save Test"
    assert len(loaded_todos) == 1
