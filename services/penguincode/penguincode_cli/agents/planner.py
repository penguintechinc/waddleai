"""Planner agent - breaks down complex tasks into actionable plans.

The planner analyzes complex user requests and creates structured plans
that can be executed by other agents (explorer, executor).

Plans are persisted to ~/.config/penguincode/plans/ so they survive
process crashes and can be reviewed or resumed later.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from penguincode_cli.ollama import Message, OllamaClient
from penguincode_cli.ui import console

from .base import AgentConfig, AgentResult

PLANS_DIR = Path.home() / ".config" / "penguincode" / "plans"

PLANNER_SYSTEM_PROMPT = """You are a planning agent for PenguinCode. Your job is to analyze complex requests and break them down into clear, actionable steps.

When given a task, create a structured plan with:

1. **Analysis**: Brief understanding of what needs to be done
2. **Steps**: Numbered list of specific, actionable steps
3. **Agent assignments**: For each step, specify which agent should handle it:
   - `explorer` - for reading, searching, understanding code
   - `executor` - for writing, editing, running commands
4. **Dependencies**: Note which steps depend on others (can run in parallel vs sequential)
5. **Estimated complexity**: simple | moderate | complex

Output your plan in this format:

```plan
ANALYSIS: <brief description of the task>

STEPS:
1. [explorer] <step description>
2. [executor] <step description>
3. [explorer|executor] <step description> (depends on: 1, 2)
...

PARALLEL_GROUPS:
- Group 1: steps 1, 2 (can run together)
- Group 2: step 3 (after group 1)
...

COMPLEXITY: <simple|moderate|complex>
```

Be thorough but concise. Each step should be specific enough for an agent to execute independently.
"""


@dataclass
class PlanStep:
    """A single step in a plan."""
    step_num: int
    agent_type: str  # "explorer" or "executor"
    description: str
    depends_on: list[int]  # Step numbers this depends on
    status: str = "pending"  # pending, completed, failed


@dataclass
class Plan:
    """A structured plan for executing a complex task."""
    analysis: str
    steps: list[PlanStep]
    parallel_groups: list[list[int]]  # Groups of step numbers that can run in parallel
    complexity: str  # simple, moderate, complex
    raw_output: str  # Original LLM output
    user_request: str = ""  # Original user request
    project_dir: str = ""  # Project directory basename
    created_at: float = field(default_factory=time.time)  # Epoch timestamp
    plan_file: Path | None = None  # Path to persisted plan file
    status: str = "created"  # created, executing, completed, failed


class PlannerAgent:
    """Agent that creates execution plans for complex tasks."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        model: str = "deepseek-coder:6.7b",
    ):
        self.client = ollama_client
        self.model = model
        self.config = AgentConfig(
            name="planner",
            model=model,
            description="Breaks down complex tasks into actionable plans",
            permissions=[],  # Planner doesn't need tools, just thinks
            system_prompt=PLANNER_SYSTEM_PROMPT,
            max_iterations=1,
        )

    async def create_plan(self, task: str, context: str = "") -> Plan:
        """
        Create a plan for a complex task.

        Args:
            task: The task to plan
            context: Optional context about the codebase or previous work

        Returns:
            Structured Plan object
        """
        console.print("[cyan]> Planning task...[/cyan]")

        messages = [
            Message(role="system", content=PLANNER_SYSTEM_PROMPT),
        ]

        if context:
            messages.append(Message(
                role="user",
                content=f"Context:\n{context}\n\nTask to plan:\n{task}"
            ))
        else:
            messages.append(Message(role="user", content=f"Task to plan:\n{task}"))

        # Get plan from LLM
        response_text = ""
        async for chunk in self.client.chat(
            model=self.model,
            messages=messages,
            stream=True,
        ):
            if chunk.message and chunk.message.content:
                response_text += chunk.message.content

        # Parse the plan
        plan = self._parse_plan(response_text)
        return plan

    def _parse_plan(self, raw_output: str) -> Plan:
        """Parse LLM output into a structured Plan."""
        lines = raw_output.split("\n")

        analysis = ""
        steps: list[PlanStep] = []
        parallel_groups: list[list[int]] = []
        complexity = "moderate"

        current_section = None

        for line in lines:
            line_stripped = line.strip()

            if line_stripped.startswith("ANALYSIS:"):
                current_section = "analysis"
                analysis = line_stripped[9:].strip()
            elif line_stripped.startswith("STEPS:"):
                current_section = "steps"
            elif line_stripped.startswith("PARALLEL_GROUPS:"):
                current_section = "parallel"
            elif line_stripped.startswith("COMPLEXITY:"):
                complexity = line_stripped[11:].strip().lower()
                if complexity not in ["simple", "moderate", "complex"]:
                    complexity = "moderate"
            elif current_section == "analysis" and line_stripped and not line_stripped.startswith(("STEPS", "PARALLEL", "COMPLEXITY")):
                analysis += " " + line_stripped
            elif current_section == "steps" and line_stripped:
                step = self._parse_step(line_stripped, len(steps) + 1)
                if step:
                    steps.append(step)
            elif current_section == "parallel" and line_stripped.startswith("- Group"):
                group = self._parse_parallel_group(line_stripped)
                if group:
                    parallel_groups.append(group)

        # If no parallel groups defined, auto-group by dependency levels
        # Steps with no dependencies run together, then steps that depend
        # only on completed groups, etc. (topological level assignment)
        if not parallel_groups and steps:
            parallel_groups = self._auto_group_steps(steps)

        return Plan(
            analysis=analysis.strip(),
            steps=steps,
            parallel_groups=parallel_groups,
            complexity=complexity,
            raw_output=raw_output,
        )

    def _parse_step(self, line: str, default_num: int) -> PlanStep | None:
        """Parse a single step line."""
        # Expected format: "1. [explorer] description (depends on: 1, 2)"
        import re

        # Try to extract step number
        num_match = re.match(r"(\d+)\.", line)
        step_num = int(num_match.group(1)) if num_match else default_num

        # Extract agent type
        agent_match = re.search(r"\[(explorer|executor)\]", line.lower())
        agent_type = agent_match.group(1) if agent_match else "executor"

        # Extract dependencies
        depends_match = re.search(r"\(depends on:\s*([\d,\s]+)\)", line.lower())
        depends_on = []
        if depends_match:
            deps_str = depends_match.group(1)
            depends_on = [int(d.strip()) for d in deps_str.split(",") if d.strip().isdigit()]

        # Extract description (remove step number, agent type, and dependencies)
        description = line
        description = re.sub(r"^\d+\.\s*", "", description)
        description = re.sub(r"\[(explorer|executor)\]\s*", "", description, flags=re.IGNORECASE)
        description = re.sub(r"\(depends on:[^)]+\)", "", description, flags=re.IGNORECASE)
        description = description.strip()

        if not description:
            return None

        return PlanStep(
            step_num=step_num,
            agent_type=agent_type,
            description=description,
            depends_on=depends_on,
        )

    def _parse_parallel_group(self, line: str) -> list[int]:
        """Parse a parallel group line."""
        import re
        # Expected format: "- Group 1: steps 1, 2 (can run together)"
        nums = re.findall(r"\d+", line.split(":")[1] if ":" in line else line)
        return [int(n) for n in nums]

    @staticmethod
    def _auto_group_steps(steps: list[PlanStep]) -> list[list[int]]:
        """
        Auto-group steps into parallel groups based on dependency levels.

        Steps with no dependencies go in group 1. Steps whose dependencies
        are all satisfied by earlier groups go in the next group. This
        maximizes parallelism while respecting ordering constraints.

        Returns:
            List of groups, each a list of step numbers that can run together
        """
        if not steps:
            return []

        step_nums = {s.step_num for s in steps}
        assigned: set[int] = set()
        groups: list[list[int]] = []

        # Iteratively assign steps whose deps are all in earlier groups
        while assigned != step_nums:
            group = []
            for s in steps:
                if s.step_num in assigned:
                    continue
                # All dependencies must either be already assigned
                # or reference steps not in this plan (treat as satisfied)
                deps_satisfied = all(
                    d in assigned or d not in step_nums
                    for d in s.depends_on
                )
                if deps_satisfied:
                    group.append(s.step_num)

            if not group:
                # Circular dependency or broken refs — dump remaining sequentially
                remaining = [s.step_num for s in steps if s.step_num not in assigned]
                groups.extend([[n] for n in remaining])
                break

            groups.append(group)
            assigned.update(group)

        return groups

    def save_plan(self, plan: Plan, project_dir: str = "") -> Path:
        """
        Persist a plan to disk as a human-readable markdown .plan file.

        Args:
            plan: The Plan to save
            project_dir: Project directory path (uses basename for filename)

        Returns:
            Path to the saved plan file
        """
        PLANS_DIR.mkdir(parents=True, exist_ok=True)

        folder_name = Path(project_dir).name if project_dir else "unknown"
        epoch64 = int(plan.created_at)
        filename = f"{folder_name}-{epoch64}.plan"
        plan_path = PLANS_DIR / filename

        plan.plan_file = plan_path
        plan.project_dir = folder_name

        content = self._render_plan_file(plan)
        plan_path.write_text(content, encoding="utf-8")

        console.print(f"[dim]Plan saved: {plan_path}[/dim]")
        return plan_path

    def update_plan_step(self, plan: Plan, step_num: int, status: str) -> None:
        """
        Update a step's status in the plan and re-write the plan file.

        Args:
            plan: The Plan to update
            step_num: Step number to update
            status: New status (completed, failed, pending)
        """
        for step in plan.steps:
            if step.step_num == step_num:
                step.status = status
                break

        # Update overall plan status
        statuses = {s.status for s in plan.steps}
        if all(s == "completed" for s in statuses):
            plan.status = "completed"
        elif "failed" in statuses:
            plan.status = "failed"
        else:
            plan.status = "executing"

        # Re-write the file if it exists
        if plan.plan_file and plan.plan_file.parent.exists():
            content = self._render_plan_file(plan)
            plan.plan_file.write_text(content, encoding="utf-8")

    def _render_plan_file(self, plan: Plan) -> str:
        """Render a Plan object as markdown for the .plan file."""
        from datetime import datetime, timezone

        ts = datetime.fromtimestamp(plan.created_at, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")

        lines = [
            "# PenguinCode Plan",
            f"- **Request**: {plan.user_request}",
            f"- **Created**: {ts_str}",
            f"- **Project**: {plan.project_dir}",
            f"- **Complexity**: {plan.complexity}",
            f"- **Status**: {plan.status}",
            "",
            "## Analysis",
            plan.analysis,
            "",
            "## Steps",
        ]

        for step in plan.steps:
            deps = f" (depends on: {', '.join(map(str, step.depends_on))})" if step.depends_on else ""
            lines.append(f"- [{step.step_num}] [{step.agent_type}] [{step.status}] {step.description}{deps}")

        lines.append("")
        lines.append("## Parallel Groups")
        for i, group in enumerate(plan.parallel_groups, 1):
            lines.append(f"- Group {i}: steps {', '.join(map(str, group))}")

        lines.append("")
        return "\n".join(lines)

    @classmethod
    def load_plan(cls, plan_path: Path) -> Plan | None:
        """
        Load a plan from a .plan file on disk.

        Args:
            plan_path: Path to the .plan file

        Returns:
            Plan object, or None if the file can't be parsed
        """
        import re

        if not plan_path.exists():
            return None

        text = plan_path.read_text(encoding="utf-8")
        lines = text.split("\n")

        user_request = ""
        project_dir = ""
        complexity = "moderate"
        plan_status = "created"
        created_at = 0.0
        analysis = ""
        steps: list[PlanStep] = []
        parallel_groups: list[list[int]] = []
        current_section = None

        for line in lines:
            stripped = line.strip()

            # Metadata fields
            if stripped.startswith("- **Request**:"):
                user_request = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- **Project**:"):
                project_dir = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- **Complexity**:"):
                complexity = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- **Status**:"):
                plan_status = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("- **Created**:"):
                from datetime import datetime, timezone
                try:
                    ts_str = stripped.split(":", 1)[1].strip()
                    dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                    created_at = dt.timestamp()
                except (ValueError, IndexError):
                    pass
            elif stripped == "## Analysis":
                current_section = "analysis"
            elif stripped == "## Steps":
                current_section = "steps"
            elif stripped == "## Parallel Groups":
                current_section = "parallel"
            elif stripped.startswith("## "):
                current_section = None
            elif current_section == "analysis" and stripped:
                analysis += (" " if analysis else "") + stripped
            elif current_section == "steps" and stripped.startswith("- ["):
                # Format: - [1] [executor] [pending] description (depends on: 1, 2)
                match = re.match(
                    r"- \[(\d+)\] \[(\w+)\] \[(\w+)\] (.+)", stripped
                )
                if match:
                    desc = match.group(4)
                    depends_on = []
                    dep_match = re.search(r"\(depends on: ([\d, ]+)\)", desc)
                    if dep_match:
                        depends_on = [int(d.strip()) for d in dep_match.group(1).split(",") if d.strip().isdigit()]
                        desc = re.sub(r"\s*\(depends on:[^)]+\)", "", desc).strip()
                    steps.append(PlanStep(
                        step_num=int(match.group(1)),
                        agent_type=match.group(2),
                        description=desc,
                        depends_on=depends_on,
                        status=match.group(3),
                    ))
            elif current_section == "parallel" and stripped.startswith("- Group"):
                nums = re.findall(r"\d+", stripped.split(":")[1] if ":" in stripped else stripped)
                if nums:
                    parallel_groups.append([int(n) for n in nums])

        if not steps:
            return None

        plan = Plan(
            analysis=analysis.strip(),
            steps=steps,
            parallel_groups=parallel_groups or [[s.step_num] for s in steps],
            complexity=complexity,
            raw_output="",
            user_request=user_request,
            project_dir=project_dir,
            created_at=created_at,
            plan_file=plan_path,
            status=plan_status,
        )
        return plan

    async def run(self, task: str, **kwargs) -> AgentResult:
        """Run the planner on a task."""
        try:
            context = kwargs.get("context", "")
            plan = await self.create_plan(task, context)

            # Format plan as readable output
            output_lines = [
                f"## Plan Analysis\n{plan.analysis}\n",
                "## Steps",
            ]

            for step in plan.steps:
                deps = f" (after steps {', '.join(map(str, step.depends_on))})" if step.depends_on else ""
                output_lines.append(f"{step.step_num}. [{step.agent_type}] {step.description}{deps}")

            output_lines.append("\n## Execution Groups (parallel)")
            for i, group in enumerate(plan.parallel_groups, 1):
                output_lines.append(f"- Group {i}: steps {', '.join(map(str, group))}")

            output_lines.append(f"\n## Complexity: {plan.complexity}")

            return AgentResult(
                agent_name="planner",
                success=True,
                output="\n".join(output_lines),
            )
        except Exception as e:
            return AgentResult(
                agent_name="planner",
                success=False,
                output="",
                error=str(e),
            )
