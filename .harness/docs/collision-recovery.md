# Harness Collision Recovery

The V2 installer refuses to overwrite existing target paths. Profile selection
must not replace project-owned agent settings or vendor configuration.

## Fresh Install Collision

When installation lists conflicts:

1. Read every path.
2. Decide whether it is project-owned, an existing V2 install, or a V1 harness.
3. For V1, use `--migrate-v1`; do not bypass the refusal.
4. For an existing V2 install, follow [upgrade.md](upgrade.md).
5. For project-owned files, either omit the conflicting profile or reconcile the
   file explicitly before retrying.

## Migration Backup

`--migrate-v1` creates a complete external snapshot before changing the target.
The successful installation also stores that snapshot under
`.harness/backups/<backup-name>/`.

Restore it with:

```bash
<blueprint-clone>/scripts/install-harness.sh \
  --rollback .harness/backups/<backup-name> <project>
```

Run rollback only with explicit human approval. Review `git status` afterward
and run the V1 gate from the restored tree.

## Never Do This

- Do not delete a dirty worktree to resolve an installation conflict.
- Do not replace `.mcp.json`, `opencode.jsonc`, `.claude/`, `.opencode/`, or
  `.gitlab/` without reviewing project-owned content.
- Do not copy an entire backup over a working V2 install except through the
  explicit rollback operation.
