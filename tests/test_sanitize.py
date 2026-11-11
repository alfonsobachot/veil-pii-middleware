"""Tests for veil_pii_middleware.adapters.command.sanitize.

Runs ``sanitize.py`` as a real subprocess -- argv list form, stdin carries the
prompt as plain data, never a constructed shell string -- to prove the
script's stdin/stdout/exit-code contract without ever letting a test's own
payload pass through a shell.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from veil_pii_middleware.adapters.command import sanitize
from veil_pii_middleware.core.vault import TokenNotFoundError, Vault

_TEST_KEY_B64 = base64.b64encode(os.urandom(32)).decode()


def _run(
    prompt: str,
    tmp_path: Path,
    *,
    vault_key: str | None = _TEST_KEY_B64,
    session_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if vault_key is None:
        env.pop("VEIL_VAULT_KEY", None)
    else:
        env["VEIL_VAULT_KEY"] = vault_key
    if session_id is None:
        env.pop("CLAUDE_SESSION_ID", None)
    else:
        env["CLAUDE_SESSION_ID"] = session_id
    return subprocess.run(
        [sys.executable, "-m", "veil_pii_middleware.adapters.command.sanitize"],
        input=prompt,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )


class TestKnownPiiTokenized:
    def test_email_and_dni_replaced_with_tokens(self, tmp_path: Path) -> None:
        result = _run("contact alice@example.com, DNI 12345678Z", tmp_path)
        assert result.returncode == 0
        assert "alice@example.com" not in result.stdout
        assert "12345678Z" not in result.stdout
        assert "PII_EMAIL_" in result.stdout
        assert "PII_DNI_" in result.stdout


class TestAuditLogWiring:
    def test_minted_token_produces_an_audit_entry_tagged_with_this_adapter(
        self, tmp_path: Path
    ) -> None:
        result = _run("contact alice@example.com", tmp_path)
        assert result.returncode == 0

        audit_path = tmp_path / ".veil" / "audit.jsonl"
        assert audit_path.exists()
        entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        assert any(e["adapter"] == "command" and e["entity_type"] == "EMAIL" for e in entries)
        assert "alice@example.com" not in audit_path.read_text(encoding="utf-8")


class TestNoPiiPassesThroughUnchanged:
    def test_plain_text_is_byte_for_byte_identical(self, tmp_path: Path) -> None:
        prompt = "just debugging a failing test, nothing sensitive here"
        result = _run(prompt, tmp_path)
        assert result.returncode == 0
        assert result.stdout == prompt


class TestAdversarialStdinNeverReachesAShell:
    def test_shell_metacharacters_cause_no_side_effect(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        payload = f'say "hi" `touch {marker}` ; rm -rf {marker} && echo pwned'
        result = _run(payload, tmp_path)
        assert result.returncode == 0
        assert not marker.exists()
        # Treated as inert text: since it contains no detectable PII, it
        # passes through unchanged rather than being interpreted.
        assert result.stdout == payload

    def test_script_never_imports_shell_execution_primitives(self) -> None:
        source = inspect.getsource(sanitize)
        for forbidden in ("subprocess", "os.system", "os.popen", "shell=True"):
            assert forbidden not in source


class TestVaultOpenFailureFailsClosed:
    def test_missing_master_key_exits_nonzero_with_empty_stdout(self, tmp_path: Path) -> None:
        result = _run("contact alice@example.com", tmp_path, vault_key=None)
        assert result.returncode != 0
        assert result.stdout == ""
        assert "alice@example.com" not in result.stdout


class TestSessionIdFallback:
    def test_missing_claude_session_id_warns_on_stderr(self, tmp_path: Path) -> None:
        result = _run("nothing sensitive here", tmp_path)
        assert result.returncode == 0
        assert "CLAUDE_SESSION_ID" in result.stderr
        assert "veil-demo" in result.stderr

    def test_explicit_session_id_does_not_warn(self, tmp_path: Path) -> None:
        result = _run("nothing sensitive here", tmp_path, session_id="session-a")
        assert result.returncode == 0
        assert "CLAUDE_SESSION_ID" not in result.stderr

    def test_different_session_ids_do_not_share_vault_mappings(self, tmp_path: Path) -> None:
        result = _run("contact alice@example.com", tmp_path, session_id="session-a")
        assert result.returncode == 0
        match = re.search(r"PII_EMAIL_\d{4}", result.stdout)
        assert match is not None
        token = match.group(0)

        db_path = tmp_path / ".veil" / "vault.db"
        key = base64.b64decode(_TEST_KEY_B64)
        with Vault(db_path, "session-a", master_key=key) as vault_a:
            assert vault_a.resolve(token) == "alice@example.com"
        with (
            Vault(db_path, "session-b", master_key=key) as vault_b,
            pytest.raises(TokenNotFoundError),
        ):
            vault_b.resolve(token)


class TestCommandFileArgumentPassing:
    def test_veil_md_only_uses_here_string_for_arguments(self) -> None:
        command_file = (
            Path(__file__).parent.parent / ".claude" / "commands" / "veil.md"
        ).read_text(encoding="utf-8")
        assert '<<< "$ARGUMENTS"' in command_file
        # Only the executable `!`...`` line may reference $ARGUMENTS outside
        # a here-string; explanatory prose inside the <!-- --> comment block
        # is not executable and is exempt from this check.
        in_comment = False
        for line in command_file.splitlines():
            if "<!--" in line:
                in_comment = True
            if "-->" in line:
                in_comment = False
                continue
            if in_comment:
                continue
            if "$ARGUMENTS" in line:
                assert '<<< "$ARGUMENTS"' in line

    def test_no_wildcard_bash_allow_list_grant(self) -> None:
        command_file = (
            Path(__file__).parent.parent / ".claude" / "commands" / "veil.md"
        ).read_text(encoding="utf-8")
        frontmatter = command_file.split("---")[1]
        assert "allowed-tools" not in frontmatter


class TestInitDocstring:
    def test_references_veil_not_stale_ask(self) -> None:
        from veil_pii_middleware.adapters import command

        assert "/veil" in (command.__doc__ or "")
        assert "/ask" not in (command.__doc__ or "")


def test_accented_prompt_survives_without_pythonutf8(tmp_path: Path) -> None:
    """Windows defaults stdin/stdout to cp1252; the prompt must still round-trip as UTF-8."""
    env = dict(os.environ)
    env["VEIL_VAULT_KEY"] = _TEST_KEY_B64
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    result = subprocess.run(
        [sys.executable, "-m", "veil_pii_middleware.adapters.command.sanitize"],
        input="Logroño: alice@example.com".encode(),
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").startswith("Logroño: PII_EMAIL_")
