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
import subprocess
import sys
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
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if vault_key is None:
        env.pop("VEIL_VAULT_KEY", None)
    else:
        env["VEIL_VAULT_KEY"] = vault_key
    return subprocess.run(
        [sys.executable, "-m", module],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )


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
            assert "from veil_pii_middleware.adapters.hooks._scope import is_demo_db_command" in (
                source
            )


class TestNoShellExecutionPrimitives:
    def test_scripts_never_import_shell_execution(self) -> None:
        for module in (pre_tool_use, post_tool_use):
            source = inspect.getsource(module)
            for forbidden in ("subprocess", "os.system", "os.popen", "shell=True"):
                assert forbidden not in source


class TestSettingsFragmentNotWiredIntoCommittedSettings:
    def test_settings_json_does_not_reference_demo_hooks(self) -> None:
        root = Path(__file__).parent.parent
        settings = (root / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert "pre_tool_use.py" not in settings
        assert "post_tool_use.py" not in settings
        assert "demo/settings.hooks.json" not in settings

    def test_settings_local_example_does_not_reference_demo_hooks(self) -> None:
        root = Path(__file__).parent.parent
        example = (root / ".claude" / "settings.local.json.example").read_text(encoding="utf-8")
        assert "pre_tool_use.py" not in example
        assert "post_tool_use.py" not in example
        assert "demo/settings.hooks.json" not in example

    def test_settings_hooks_json_is_valid_and_hooks_only(self) -> None:
        root = Path(__file__).parent.parent
        data = json.loads((root / "demo" / "settings.hooks.json").read_text(encoding="utf-8"))
        keys = {k for k in data if not k.startswith("$") and not k.startswith("//")}
        assert keys == {"hooks"}


class TestGitignoreCoversVault:
    def test_veil_directory_is_ignored(self) -> None:
        root = Path(__file__).parent.parent
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".veil/" in gitignore
