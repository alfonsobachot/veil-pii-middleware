# Harness Upgrade Guide

V2 records its installed version, selected profiles, owned paths, and source
hashes in `.harness/manifest.json`.

## Upgrade Flow

1. Start from a clean target work tree.
2. Clone the desired blueprint version into a temporary directory.
3. Review the currently installed profiles:

   ```bash
   python3 -c 'import json; print(",".join(json.load(open(".harness/manifest.json"))["profiles"][1:]))'
   ```

4. Preview the install with the same profiles:

   ```bash
   <tmp>/scripts/install-harness.sh --dry-run --profile <profiles> <project>
   ```

5. The installer refuses owned-file collisions during a normal fresh install.
   Use the release-specific migration procedure when changing major versions;
   do not replace files opaquely.
6. Run `<tmp>/scripts/install-harness.sh --verify <project>`.
7. Run `.harness/bin/verify`; exit `0` is required before merge.

## V1 Migration

Preview first:

```bash
<tmp>/scripts/install-harness.sh --dry-run --migrate-v1 \
  --profile <profiles> <project>
```

Then run the same command without `--dry-run`. The installer writes a complete
external backup before changing the target and copies it under
`.harness/backups/` after installation.

The migration preserves task state, memory, configured project facts, configured
gate rows, and canonical custom skills. Review adapter customizations in the
backup before deleting it.

## Rules

- Never edit `.harness/manifest.json` to claim an upgrade.
- Never copy an old settings file wholesale over a newer one.
- Never put secrets or local credentials in tracked harness files.
