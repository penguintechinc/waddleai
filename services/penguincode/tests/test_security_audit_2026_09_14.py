"""Regression tests for the 2026-09-14 security audit of services/penguincode.

Each test pins one audit finding in the agent tool layer: the threat model is
indirect prompt injection, where untrusted repository content steers the agent
into a destructive or out-of-tree operation.

# regression: audit-2026-09-14
"""

import hmac
import time

import grpc
import pytest

from penguincode_cli.client.tool_executor import LocalToolExecutor
from penguincode_cli.config.settings import AuthConfig
from penguincode_cli.proto import AuthRequest, RefreshRequest
from penguincode_cli.server.services.auth import AuthServiceImpl
from penguincode_cli.shared.command_safety import classify_destructive
from penguincode_cli.tools.bash import BashTool


class AbortCalled(Exception):
    """Raised by the fake gRPC context in place of a real abort."""

    def __init__(self, code, details):
        super().__init__(details)
        self.code = code
        self.details = details


class FakeContext:
    """Minimal grpc.aio.ServicerContext stand-in whose abort() raises, as gRPC's does."""

    def __init__(self):
        self.aborts: list[tuple[object, str]] = []

    async def abort(self, code, details):
        self.aborts.append((code, details))
        raise AbortCalled(code, details)


# ── Finding 1: command injection in the grep tool ───────────────────


class TestGrepCommandInjection:
    """grep must never let an agent-supplied pattern reach a shell. # regression: audit-2026-09-14"""

    @pytest.mark.asyncio
    async def test_quote_escape_in_pattern_executes_nothing(self, tmp_path):
        """A pattern breaking out of the single quotes must not run a command."""
        (tmp_path / "haystack.txt").write_text("needle here\n")
        sentinel = tmp_path / "pwned.sentinel"

        executor = LocalToolExecutor(working_dir=str(tmp_path))
        # Closes the opening quote, chains a command, reopens a quote so the
        # pre-fix f-string stayed syntactically valid shell.
        pattern = f"needle' ; touch '{sentinel}' ; echo 'x"

        result = await executor.execute("grep", {"pattern": pattern, "path": "."})

        assert not sentinel.exists(), "injected command executed — pattern reached a shell"
        assert result is not None

    @pytest.mark.asyncio
    async def test_command_substitution_in_pattern_is_not_evaluated(self, tmp_path):
        """A pattern closing the quote around a $(...) must not be evaluated."""
        sentinel = tmp_path / "substituted.sentinel"
        (tmp_path / "haystack.txt").write_text("nothing interesting\n")

        executor = LocalToolExecutor(working_dir=str(tmp_path))
        pattern = f"needle'$(touch {sentinel})'x"

        await executor.execute("grep", {"pattern": pattern, "path": "."})

        assert not sentinel.exists(), "command substitution was evaluated"

    @pytest.mark.asyncio
    async def test_benign_grep_still_finds_matches(self, tmp_path):
        """Negative control: the tool still does its job after the fix."""
        (tmp_path / "haystack.txt").write_text("alpha\nbeta needle gamma\n")

        executor = LocalToolExecutor(working_dir=str(tmp_path))
        result = await executor.execute("grep", {"pattern": "needle", "path": "."})

        assert result.success is True
        assert "needle" in result.data


# ── Finding 2: no path containment in the file tools ────────────────


class TestPathContainment:
    """Every file tool must stay inside the working directory. # regression: audit-2026-09-14"""

    @pytest.mark.asyncio
    async def test_absolute_path_outside_tree_is_refused(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        outside = tmp_path / "outside.secret"
        outside.write_text("id_rsa contents")

        executor = LocalToolExecutor(working_dir=str(workdir))
        result = await executor.execute("read", {"path": str(outside)})

        assert result.success is False
        assert "outside the working directory" in (result.error or "")
        assert "id_rsa contents" not in (result.data or "")

    @pytest.mark.asyncio
    async def test_dotdot_traversal_is_refused(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        outside = tmp_path / "outside.secret"
        outside.write_text("id_rsa contents")

        executor = LocalToolExecutor(working_dir=str(workdir))
        result = await executor.execute("read", {"path": f"../{outside.name}"})

        assert result.success is False
        assert "outside the working directory" in (result.error or "")

    @pytest.mark.asyncio
    async def test_write_cannot_escape_the_tree(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        target = tmp_path / "escaped.txt"

        executor = LocalToolExecutor(working_dir=str(workdir))
        result = await executor.execute("write", {"path": "../escaped.txt", "content": "owned"})

        assert result.success is False
        assert not target.exists(), "write escaped the working directory"

    @pytest.mark.asyncio
    async def test_symlink_escape_is_refused(self, tmp_path):
        """A symlink living inside the tree must not be a way out of it."""
        workdir = tmp_path / "work"
        workdir.mkdir()
        outside = tmp_path / "outside.secret"
        outside.write_text("id_rsa contents")
        (workdir / "innocent.txt").symlink_to(outside)

        executor = LocalToolExecutor(working_dir=str(workdir))
        result = await executor.execute("read", {"path": "innocent.txt"})

        assert result.success is False
        assert "id_rsa contents" not in (result.data or "")

    @pytest.mark.asyncio
    async def test_legitimate_relative_path_still_works(self, tmp_path):
        """Negative control: ordinary in-tree work is unaffected."""
        workdir = tmp_path / "work"
        (workdir / "src").mkdir(parents=True)
        (workdir / "src" / "main.py").write_text("print('hi')\n")

        executor = LocalToolExecutor(working_dir=str(workdir))
        result = await executor.execute("read", {"path": "src/main.py"})

        assert result.success is True
        assert "print('hi')" in result.data


# ── Finding 3: the destructive-command guard was never called ───────


class TestDestructiveCommandGuard:
    """is_destructive() must gate execution, not merely exist. # regression: audit-2026-09-14"""

    @pytest.mark.asyncio
    async def test_bash_tool_refuses_destructive_command(self, tmp_path):
        victim = tmp_path / "important.txt"
        victim.write_text("do not delete")

        tool = BashTool(working_dir=str(tmp_path))
        result = await tool.execute(f"rm -f {victim}")

        assert result.success is False
        assert "destructive" in (result.error or "").lower()
        assert victim.exists(), "destructive command ran despite the guard"

    @pytest.mark.asyncio
    async def test_bash_tool_runs_benign_command(self, tmp_path):
        """Negative control: the guard does not block ordinary commands."""
        tool = BashTool(working_dir=str(tmp_path))
        result = await tool.execute("echo penguincode")

        assert result.success is True
        assert "penguincode" in result.data

    @pytest.mark.asyncio
    async def test_confirmation_callback_can_approve(self, tmp_path):
        """An explicit approval path exists — the guard blocks, it does not forbid."""
        victim = tmp_path / "scratch.txt"
        victim.write_text("temporary")
        seen: list[tuple[str, str]] = []

        async def approve(command: str, keyword: str) -> bool:
            seen.append((command, keyword))
            return True

        tool = BashTool(working_dir=str(tmp_path), confirm_destructive=approve)
        result = await tool.execute(f"rm -f {victim}")

        assert seen and seen[0][1] == "rm"
        assert result.success is True
        assert not victim.exists()

    @pytest.mark.asyncio
    async def test_confirmation_callback_can_deny(self, tmp_path):
        victim = tmp_path / "scratch.txt"
        victim.write_text("temporary")

        async def deny(command: str, keyword: str) -> bool:
            return False

        tool = BashTool(working_dir=str(tmp_path), confirm_destructive=deny)
        result = await tool.execute(f"rm -f {victim}")

        assert result.success is False
        assert victim.exists()

    @pytest.mark.asyncio
    async def test_is_destructive_classifies_consistently(self):
        tool = BashTool()
        assert tool.is_destructive("sudo rm -rf /") is True
        assert tool.is_destructive("chmod 777 /etc/shadow") is True
        assert tool.is_destructive("dd if=/dev/zero of=/dev/sda") is True
        assert tool.is_destructive("ls -la") is False
        assert tool.is_destructive("python -m pytest") is False

    @pytest.mark.asyncio
    async def test_client_executor_bash_is_gated_too(self, tmp_path):
        """LocalToolExecutor._execute_bash had no gate at all — it must share this one."""
        victim = tmp_path / "important.txt"
        victim.write_text("do not delete")

        executor = LocalToolExecutor(working_dir=str(tmp_path))
        result = await executor.execute("bash", {"command": f"rm -f {victim}"})

        assert result.success is False
        assert victim.exists(), "destructive command ran on the client executor path"

    @pytest.mark.asyncio
    async def test_client_executor_runs_benign_command(self, tmp_path):
        executor = LocalToolExecutor(working_dir=str(tmp_path))
        result = await executor.execute("bash", {"command": "echo penguincode"})

        assert result.success is True
        assert "penguincode" in result.data


# ── Finding 3 (precision): the guard must not fire on everyday commands ──

# Real commands from this repo and its day-to-day workflow. Every one of these
# was refused by the original substring classifier ("skill" contains "kill",
# "--format" contains "format", any ">" is a redirect). With no confirmation UI,
# a refusal is outright, so a single false positive here makes the gate
# unusable — and an unusable gate gets switched off, real protection included.
BENIGN_COMMANDS = [
    "pytest tests/test_skill_system.py",
    "ls .claude/skills/",
    "ruff format --check .",
    "npm run format",
    "git log --format=%h -5",
    "pytest -q > out.txt",
    "grep -rn 'a->b' src/",
    "python -c 'print(1>2)'",
    "ls -la",
    "python -m pytest",
    "make lint && make test",
    "cat README.md | head -20",
    "git diff --stat",
    "echo 'notes' > /tmp/scratch.txt",
    "npm run build 2> build.log",
    "grep -rn 'kill' penguincode_cli/",
    "black --check .",
    "docker compose config",
    "pytest -k 'skills and not slow'",
    "git commit -m 'chore: reformat'",
]

# Detection must stay at zero misses on these.
DESTRUCTIVE_COMMANDS = [
    "rm -rf /",
    "sudo reboot",
    "dd if=/dev/zero of=/dev/sda",
    "chmod 777 /etc",
    "mkfs.ext4 /dev/sdb",
    "sudo rm -rf /",
    "chmod 777 /etc/shadow",
    "rm -f important.txt",
    "/bin/rm -rf /home",
    "\\rm -rf /",
    'r""m -rf /',
    "env FOO=bar rm -rf /tmp/x",
    "timeout 30 rm -rf /var",
    "find . -name '*.py' -exec rm {} ;",
    "ls && rm -rf /",
    "cat targets.txt | xargs rm",
    "echo boom > /dev/sda",
    "echo root::0:0 > /etc/passwd",
    "pkill -9 python",
    "shutdown -h now",
    "chown -R root /",
    "su - root",
    "rmdir /var/lib",
]


class TestDestructiveClassifierPrecision:
    """Command-name matching, not substring matching. # regression: audit-2026-09-14"""

    def test_no_false_positives_on_benign_commands(self):
        assert BENIGN_COMMANDS, "benign corpus is empty — the check would prove nothing"

        refused = {cmd: classify_destructive(cmd) for cmd in BENIGN_COMMANDS}
        false_positives = {cmd: hit for cmd, hit in refused.items() if hit is not None}

        assert not false_positives, (
            f"{len(false_positives)} of {len(BENIGN_COMMANDS)} benign commands refused: "
            f"{false_positives}"
        )

    def test_no_misses_on_destructive_commands(self):
        assert DESTRUCTIVE_COMMANDS, "destructive corpus is empty — the check would prove nothing"

        classified = {cmd: classify_destructive(cmd) for cmd in DESTRUCTIVE_COMMANDS}
        misses = [cmd for cmd, hit in classified.items() if hit is None]

        assert not misses, f"{len(misses)} of {len(DESTRUCTIVE_COMMANDS)} destructive commands missed: {misses}"

    def test_dangerous_names_are_matched_as_commands_not_substrings(self):
        """The specific collisions that made the original classifier unusable."""
        assert classify_destructive("pytest tests/test_skill_system.py") is None  # 'skill' ⊃ 'kill'
        assert classify_destructive("ruff format --check .") is None  # '--format' ⊃ 'format'
        assert classify_destructive("git log --format=%h") is None
        assert classify_destructive("echo 'model deleted'") is None  # 'deleted' ⊃ 'del '
        # ...while the real commands still match
        assert classify_destructive("kill -9 1234") == "kill"
        assert classify_destructive("format C:") == "format"
        assert classify_destructive("del data.txt") == "del"

    def test_path_and_escape_forms_are_resolved(self):
        """Hiding the name from a naive substring match must not hide it from this."""
        assert classify_destructive("/bin/rm -rf /home") == "rm"
        assert classify_destructive("./rm -rf x") == "rm"
        assert classify_destructive("\\rm -rf /") == "rm"

    def test_redirects_are_scoped_to_system_paths(self):
        """A bare redirect is ordinary; a redirect into /dev or /etc is not."""
        assert classify_destructive("pytest -q > out.txt") is None
        assert classify_destructive("make build >> build.log") is None
        assert classify_destructive("echo boom > /dev/sda") == ">"
        assert classify_destructive("echo x >> /etc/hosts") == ">"

    def test_every_pipeline_segment_is_inspected(self):
        assert classify_destructive("ls && rm -rf /") == "rm"
        assert classify_destructive("cat list | xargs rm") == "rm"
        assert classify_destructive("echo hi; sudo reboot") == "sudo"

    def test_documented_evasions_are_not_claimed_to_be_caught(self):
        """This is a keyword gate, not a sandbox — pin the honest limits.

        If a future change starts catching these, that is good news and this test
        should be updated; what it prevents is the module claiming containment it
        does not provide.
        """
        assert classify_destructive("echo cm0gLXJmIC8= | base64 -d | sh") is None
        assert classify_destructive("python -c \"import os; os.system('rm -rf /')\"") is None
        assert classify_destructive("find . -delete") is None
        assert classify_destructive("$RM -rf /") is None


# ── Findings 4 & 5: refresh token lifecycle and credential comparison ──


def _auth_config(**overrides) -> AuthConfig:
    defaults = dict(
        enabled=True,
        jwt_secret="unit-test-secret-that-is-long-enough-for-hs256",
        shared_key="team-shared-key",
        api_keys=["api-key-1"],
        token_expiry=3600,
        refresh_expiry=86400,
    )
    defaults.update(overrides)
    return AuthConfig(**defaults)


class TestRefreshTokenLifecycle:
    """refresh_expiry must be enforced and replays must revoke. # regression: audit-2026-09-14"""

    @pytest.mark.asyncio
    async def test_expired_refresh_token_is_refused(self):
        service = AuthServiceImpl(_auth_config(refresh_expiry=0))
        context = FakeContext()

        auth = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )

        with pytest.raises(AbortCalled) as excinfo:
            await service.RefreshToken(RefreshRequest(refresh_token=auth.refresh_token), context)

        assert excinfo.value.code == grpc.StatusCode.UNAUTHENTICATED
        assert "expired" in excinfo.value.details.lower()

    @pytest.mark.asyncio
    async def test_unexpired_refresh_token_still_works(self):
        """Negative control: a live refresh token rotates normally."""
        service = AuthServiceImpl(_auth_config(refresh_expiry=3600))
        context = FakeContext()

        auth = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )
        refreshed = await service.RefreshToken(
            RefreshRequest(refresh_token=auth.refresh_token), context
        )

        assert refreshed.access_token
        assert refreshed.refresh_token != auth.refresh_token

    @pytest.mark.asyncio
    async def test_refresh_token_carries_the_configured_expiry(self):
        service = AuthServiceImpl(_auth_config(refresh_expiry=1234))
        context = FakeContext()

        auth = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )

        _user_id, expires_at = service._refresh_tokens[auth.refresh_token]
        assert 1230 <= expires_at - int(time.time()) <= 1234

    @pytest.mark.asyncio
    async def test_replayed_token_revokes_the_whole_chain(self):
        """A rotated-out token presented again means the chain leaked."""
        service = AuthServiceImpl(_auth_config())
        context = FakeContext()

        auth = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )
        rotated = await service.RefreshToken(
            RefreshRequest(refresh_token=auth.refresh_token), context
        )

        # Attacker replays the already-rotated token.
        with pytest.raises(AbortCalled) as excinfo:
            await service.RefreshToken(RefreshRequest(refresh_token=auth.refresh_token), context)
        assert "reuse" in excinfo.value.details.lower()

        # The legitimate holder's current token is revoked as well.
        with pytest.raises(AbortCalled):
            await service.RefreshToken(RefreshRequest(refresh_token=rotated.refresh_token), context)
        assert service._refresh_tokens == {}

    @pytest.mark.asyncio
    async def test_reuse_detection_is_logged(self, caplog):
        service = AuthServiceImpl(_auth_config())
        context = FakeContext()

        auth = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )
        await service.RefreshToken(RefreshRequest(refresh_token=auth.refresh_token), context)

        with caplog.at_level("ERROR"):
            with pytest.raises(AbortCalled):
                await service.RefreshToken(
                    RefreshRequest(refresh_token=auth.refresh_token), context
                )

        assert any(
            "reuse detected" in record.getMessage().lower() for record in caplog.records
        ), "token reuse was not logged"

    @pytest.mark.asyncio
    async def test_one_users_reuse_does_not_revoke_another(self):
        service = AuthServiceImpl(_auth_config())
        context = FakeContext()

        victim = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )
        bystander = await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-2"), context
        )
        await service.RefreshToken(RefreshRequest(refresh_token=victim.refresh_token), context)

        with pytest.raises(AbortCalled):
            await service.RefreshToken(RefreshRequest(refresh_token=victim.refresh_token), context)

        still_valid = await service.RefreshToken(
            RefreshRequest(refresh_token=bystander.refresh_token), context
        )
        assert still_valid.access_token


class TestConstantTimeCredentialComparison:
    """Authentication must compare credentials with hmac.compare_digest. # regression: audit-2026-09-14"""

    @pytest.mark.asyncio
    async def test_authenticate_uses_compare_digest(self, monkeypatch):
        import penguincode_cli.server.services.auth as auth_module

        calls: list[tuple[bytes, bytes]] = []
        real_compare = hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real_compare(a, b)

        monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)

        service = AuthServiceImpl(_auth_config())
        context = FakeContext()
        await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )

        assert calls, "credential comparison did not go through hmac.compare_digest"

    @pytest.mark.asyncio
    async def test_all_configured_keys_are_compared(self, monkeypatch):
        """No short-circuit: which key matched must not be observable via timing."""
        import penguincode_cli.server.services.auth as auth_module

        calls: list[tuple[bytes, bytes]] = []
        real_compare = hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real_compare(a, b)

        monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)

        service = AuthServiceImpl(_auth_config(api_keys=["api-key-1", "api-key-2"]))
        context = FakeContext()
        await service.Authenticate(
            AuthRequest(api_key="team-shared-key", client_id="user-1"), context
        )

        # shared key + both API keys
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_valid_and_invalid_credentials_still_behave(self):
        """Negative control: the constant-time path preserves auth semantics."""
        service = AuthServiceImpl(_auth_config())
        context = FakeContext()

        assert (
            await service.Authenticate(AuthRequest(api_key="api-key-1", client_id="u"), context)
        ).access_token

        with pytest.raises(AbortCalled) as excinfo:
            await service.Authenticate(AuthRequest(api_key="wrong-key", client_id="u"), context)
        assert excinfo.value.code == grpc.StatusCode.UNAUTHENTICATED
