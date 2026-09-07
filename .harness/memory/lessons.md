# Harness Lessons

Persistent project lessons distilled from `.harness/memory/events.jsonl`.

Agents may append memory events and propose distilled lessons. Humans review
changes before they become durable team guidance.

## Distilled Patterns

### lesson
- ThreadingHTTPServer adapters need explicit response-commit tracking before any second send_response on failure Prevention: Any adapter that streams a response back to a client must track whether the status line/headers have already been sent (a boolean flag) before deciding whether a failure path may still write an error response of its own; once committed, stop and let the connection close instead. Source: `.harness/handoff.md`
- core/vault.py's Vault._next_seq races under real thread concurrency; fix by serializing the caller, not the shared core module Prevention: When a mission's non-goals forbid touching core/, and a core module has a concurrency assumption a new adapter breaks, serialize access from the adapter side (e.g. a process-local threading.Lock around the vault call) rather than expanding mission scope to patch core/. Source: `.harness/handoff.md`

### procedure
- Live spikes against a real Claude Code session must write evidence into handoff.md during the same pass, not just narrate it in chat Prevention: Write handoff.md with concrete spike evidence (proxy logs, exact commands, decrypted verification results) as part of the same implementation pass that runs the spike -- before requesting independent review -- not as a final step after review comes back. Source: `.harness/handoff.md`
