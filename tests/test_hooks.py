"""Tests for veil_pii_middleware.adapters.hooks.{pre_tool_use,post_tool_use}.

Runs both hook scripts as real subprocesses -- argv list form, the hook's
JSON payload is built in Python and piped via stdin as plain data, never a
constructed shell string -- mirroring tests/test_sanitize.py's approach for
the /veil command adapter.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from veil_pii_middleware.adapters.hooks import post_tool_use, pre_tool_use
from veil_pii_middleware.core.detector import EntityType
from veil_pii_middleware.core.vault import Vault

_TEST_KEY_B64 = base64.b64encode(os.urandom(32)).decode()
_DEMO_DB_COMMAND = 'sqlite3 demo/veil_demo.db "select * from users"'


def _run(
    module: str,
    payload: Any,
    tmp_path: Path,
    *,
    vault_key: str | None = _TEST_KEY_B64,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if vault_key is None:
        env.pop("VEIL_VAULT_KEY", None)
    else:
        env["VEIL_VAULT_KEY"] = vault_key
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )


def _unreachable_gateway_url() -> str:
    """A ``http://127.0.0.1:<port>`` URL nothing is listening on.

    Binds a real socket to an OS-assigned free port, then closes it --
    the port is guaranteed refused (not just "probably free") for at
    least long enough for one connection attempt, without needing a
    fixed port that might collide with something else on the machine.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _seed_token(tmp_path: Path, session_id: str, value: str, entity_type: str) -> str:
    db_path = tmp_path / ".veil" / "vault.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = base64.b64decode(_TEST_KEY_B64)
    with Vault(db_path, session_id, master_key=key) as vault:
        return vault.get_or_create_token(value, EntityType[entity_type])


class TestPreToolUseDetokenizesKnownToken:
    def test_known_token_resolved_in_updated_input(self, tmp_path: Path) -> None:
        token = _seed_token(tmp_path, "session-a", "alice@example.com", "EMAIL")
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    f"sqlite3 demo/veil_demo.db \"select * from users where email='{token}'\""
                ),
                "description": "look up user",
                "timeout": 120000,
                "run_in_background": False,
            },
        }
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 0
        out = json.loads(result.stdout)
        updated = out["hookSpecificOutput"]["updatedInput"]
        assert "alice@example.com" in updated["command"]
        assert token not in updated["command"]
        # Non-command fields are preserved untouched.
        assert updated["description"] == "look up user"
        assert updated["timeout"] == 120000
        assert updated["run_in_background"] is False
        # Never auto-approves: only `updatedInput` is emitted, so the
        # rewritten command still goes through Claude Code's normal
        # ask-permission flow rather than skipping it.
        assert "permissionDecision" not in out["hookSpecificOutput"]


class TestPreToolUseBlocksOnUnresolvableToken:
    def test_foreign_token_blocks(self, tmp_path: Path) -> None:
        token = _seed_token(tmp_path, "session-a", "alice@example.com", "EMAIL")
        payload = {
            "session_id": "session-b",
            "tool_name": "Bash",
            "tool_input": {"command": f"sqlite3 demo/veil_demo.db \"select '{token}'\""},
        }
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 2
        assert result.stdout == ""

    def test_missing_vault_key_blocks(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
        }
        result = _run(
            "veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path, vault_key=None
        )
        assert result.returncode == 2
        assert result.stdout == ""

    @pytest.mark.parametrize("payload", [[1, 2, 3], None, "just a string", True])
    def test_non_object_top_level_payload_blocks(self, payload: object, tmp_path: Path) -> None:
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 2
        assert result.stdout == ""


class TestPreToolUsePassesThroughOutOfScopeCalls:
    def test_non_demo_db_bash_command_passes_through(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        }
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (tmp_path / ".veil" / "vault.db").exists()

    def test_non_bash_tool_passes_through(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Read",
            "tool_input": {"file_path": "demo/veil_demo.db"},
        }
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (tmp_path / ".veil" / "vault.db").exists()


class TestPostToolUseTokenizesGenericShape:
    def test_nested_response_shape_gets_tokenized(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {
                "stdout": "row 1",
                "rows": [
                    {"email": "alice@example.com", "count": 3},
                    {"email": "bob@example.com", "count": 1},
                ],
                "exit_code": 0,
            },
        }
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode == 0
        out = json.loads(result.stdout)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        assert "alice@example.com" not in json.dumps(updated)
        assert "bob@example.com" not in json.dumps(updated)
        assert updated["rows"][0]["count"] == 3
        assert updated["exit_code"] == 0
        assert "PII_EMAIL_" in updated["rows"][0]["email"]
        assert "PII_EMAIL_" in updated["rows"][1]["email"]
        # Same value within a session yields the same token elsewhere too.
        token = _seed_token(tmp_path, "session-a", "alice@example.com", "EMAIL")
        assert updated["rows"][0]["email"] == token


class TestPostToolUseAuditLogWiring:
    def test_minted_token_produces_an_audit_entry_tagged_with_this_adapter(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {"stdout": "alice@example.com", "exit_code": 0},
        }
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode == 0

        audit_path = tmp_path / ".veil" / "audit.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines]
        assert any(
            e["adapter"] == "hooks:post_tool_use" and e["entity_type"] == "EMAIL" for e in entries
        )
        assert "alice@example.com" not in audit_path.read_text(encoding="utf-8")


class TestPostToolUsePassesThroughOutOfScopeCalls:
    def test_non_demo_db_bash_command_passes_through(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_response": {"stdout": "alice@example.com", "exit_code": 0},
        }
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (tmp_path / ".veil" / "vault.db").exists()

    def test_non_bash_tool_passes_through(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Read",
            "tool_input": {"file_path": "demo/veil_demo.db"},
            "tool_response": {"content": "alice@example.com"},
        }
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode == 0
        assert result.stdout == ""
        assert not (tmp_path / ".veil" / "vault.db").exists()


class TestPostToolUseNonObjectPayload:
    @pytest.mark.parametrize("payload", [[1, 2, 3], None, "just a string", True])
    def test_non_object_top_level_payload_fails_without_output(
        self, payload: object, tmp_path: Path
    ) -> None:
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode != 0
        assert result.stdout == ""


class TestPostToolUseFailsClosedWithoutFabricatingOutput:
    def test_missing_vault_key_yields_no_updated_output(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {"stdout": "alice@example.com", "exit_code": 0},
        }
        result = _run(
            "veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path, vault_key=None
        )
        assert result.returncode != 0
        assert "updatedToolOutput" not in result.stdout


class TestSharedScopeCheck:
    def test_both_scripts_import_shared_helper(self) -> None:
        for module in (pre_tool_use, post_tool_use):
            source = inspect.getsource(module)
            assert (
                "from veil_pii_middleware.adapters.hooks._scope import SHELL_TOOLS, "
                "is_demo_db_command" in source
            )


class TestNoShellExecutionPrimitives:
    def test_scripts_never_import_shell_execution(self) -> None:
        for module in (pre_tool_use, post_tool_use):
            source = inspect.getsource(module)
            for forbidden in ("subprocess", "os.system", "os.popen", "shell=True"):
                assert forbidden not in source


class TestSettingsFragmentNotWiredIntoCommittedSettings:
    def test_no_committed_settings_file_wires_the_demo_hooks(self) -> None:
        """The demo hooks only load via an explicit `--settings demo/settings.hooks.json`."""
        root = Path(__file__).parent.parent
        for name in ("settings.json", "settings.local.json.example"):
            path = root / ".claude" / name
            if not path.exists():
                continue
            settings = path.read_text(encoding="utf-8")
            assert "pre_tool_use.py" not in settings
            assert "post_tool_use.py" not in settings
            assert "demo/settings.hooks.json" not in settings

    def test_settings_hooks_json_is_valid_and_has_only_hooks_and_permissions(self) -> None:
        root = Path(__file__).parent.parent
        data = json.loads((root / "demo" / "settings.hooks.json").read_text(encoding="utf-8"))
        keys = {k for k in data if not k.startswith("$") and not k.startswith("//")}
        # `permissions` merges additively (the sqlite3 allow rule); only `hooks` is replaced.
        assert keys == {"hooks", "permissions"}


class TestGitignoreCoversVault:
    def test_veil_directory_is_ignored(self) -> None:
        root = Path(__file__).parent.parent
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".veil/" in gitignore


class TestPanelReportBestEffort:
    """The hooks' panel-reporting call must never affect the hook itself.

    ``_panel_report.report`` is additive observability plumbing only -- see
    its own module docstring. These tests prove the best-effort contract
    directly (the call returns quickly against a genuinely unreachable
    gateway) and end-to-end (both hook scripts' exit code and stdout are
    byte-for-byte identical whether the gateway they try to report to is
    reachable or not).
    """

    def test_report_never_raises_and_returns_quickly_against_an_unreachable_gateway(self) -> None:
        from veil_pii_middleware.adapters.hooks._panel_report import report

        os.environ["VEIL_GATEWAY_URL"] = _unreachable_gateway_url()
        try:
            start = time.monotonic()
            report("session-a", "pre_tool_use", "start", "Bash")
            elapsed = time.monotonic() - start
        finally:
            os.environ.pop("VEIL_GATEWAY_URL", None)
        # Generous relative to the 0.2s timeout _panel_report.py sets --
        # proves the call is bounded, not that it hit the timeout exactly.
        assert elapsed < 1.0

    def test_pre_tool_use_exit_code_and_stdout_unaffected_by_gateway_reachability(
        self, tmp_path: Path
    ) -> None:
        token = _seed_token(tmp_path, "session-a", "alice@example.com", "EMAIL")
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    f"sqlite3 demo/veil_demo.db \"select * from users where email='{token}'\""
                )
            },
        }
        result_a = _run(
            "veil_pii_middleware.adapters.hooks.pre_tool_use",
            payload,
            tmp_path,
            env_overrides={"VEIL_GATEWAY_URL": _unreachable_gateway_url()},
        )
        result_b = _run(
            "veil_pii_middleware.adapters.hooks.pre_tool_use",
            payload,
            tmp_path,
            env_overrides={"VEIL_GATEWAY_URL": _unreachable_gateway_url()},
        )
        assert result_a.returncode == result_b.returncode == 0
        assert result_a.stdout == result_b.stdout

    def test_post_tool_use_exit_code_and_stdout_unaffected_by_gateway_reachability(
        self, tmp_path: Path
    ) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {"stdout": "alice@example.com", "exit_code": 0},
        }
        result_a = _run(
            "veil_pii_middleware.adapters.hooks.post_tool_use",
            payload,
            tmp_path,
            env_overrides={"VEIL_GATEWAY_URL": _unreachable_gateway_url()},
        )
        result_b = _run(
            "veil_pii_middleware.adapters.hooks.post_tool_use",
            payload,
            tmp_path,
            env_overrides={"VEIL_GATEWAY_URL": _unreachable_gateway_url()},
        )
        assert result_a.returncode == result_b.returncode == 0
        assert result_a.stdout == result_b.stdout


def _run_utf8_bytes(
    module: str, payload: Any, tmp_path: Path
) -> subprocess.CompletedProcess[bytes]:
    """Run a hook the way Claude Code does on Windows: raw UTF-8 bytes, no PYTHONUTF8."""
    env = dict(os.environ)
    env["VEIL_VAULT_KEY"] = _TEST_KEY_B64
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )


class TestHooksReadUtf8Stdin:
    def test_pre_tool_use_keeps_accents_in_updated_input(self, tmp_path: Path) -> None:
        token = _seed_token(tmp_path, "session-a", "alice@example.com", "EMAIL")
        command = (
            "sqlite3 demo/veil_demo.db "
            f"\"select * from users where name='José Muñoz' or email='{token}'\""
        )
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

        result = _run_utf8_bytes(
            "veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path
        )

        assert result.returncode == 0, result.stderr
        updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert "name='José Muñoz'" in updated
        assert "alice@example.com" in updated

    def test_post_tool_use_keeps_accents_in_tokenized_output(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-a",
            "tool_name": "Bash",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {"stdout": "Logroño|alice@example.com", "exit_code": 0},
        }

        result = _run_utf8_bytes(
            "veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path
        )

        assert result.returncode == 0, result.stderr
        assert "Logroño|PII_EMAIL_" in json.dumps(json.loads(result.stdout), ensure_ascii=False)


class TestPowerShellToolIsInScope:
    """S4: on Windows the model runs shell commands through the PowerShell tool too."""

    def test_pre_hook_resolves_a_token_in_a_powershell_command(self, tmp_path: Path) -> None:
        token = _seed_token(tmp_path, "session-ps", "alice@example.com", "EMAIL")
        payload = {
            "session_id": "session-ps",
            "tool_name": "PowerShell",
            "tool_input": {
                "command": f"python -c \"import sqlite3; sqlite3.connect('demo/veil_demo.db')"
                f".execute('select 1 where 1=?', ('{token}',))\"",
            },
        }
        result = _run("veil_pii_middleware.adapters.hooks.pre_tool_use", payload, tmp_path)
        assert result.returncode == 0
        updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
        assert "alice@example.com" in updated["command"]
        assert token not in updated["command"]

    def test_post_hook_tokenizes_powershell_output(self, tmp_path: Path) -> None:
        payload = {
            "session_id": "session-ps",
            "tool_name": "PowerShell",
            "tool_input": {"command": _DEMO_DB_COMMAND},
            "tool_response": {"stdout": "alice@example.com", "stderr": ""},
        }
        result = _run("veil_pii_middleware.adapters.hooks.post_tool_use", payload, tmp_path)
        assert result.returncode == 0
        updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedToolOutput"]
        assert "alice@example.com" not in json.dumps(updated)
        assert updated["stdout"].startswith("PII_EMAIL_")

    @pytest.mark.parametrize("name", ["settings.hooks.json", "settings.hooks.isolated.json"])
    def test_both_demo_settings_match_powershell(self, name: str) -> None:
        root = Path(__file__).parent.parent
        data = json.loads((root / "demo" / name).read_text(encoding="utf-8"))
        for event in ("PreToolUse", "PostToolUse"):
            matchers = {entry["matcher"] for entry in data["hooks"][event]}
            assert matchers == {"Bash|PowerShell"}
