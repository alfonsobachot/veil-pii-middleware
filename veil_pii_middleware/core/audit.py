"""Append-only JSONL audit log.

Records timestamp, token, entity type, detector, offset, hash of the value and
originating adapter. It never contains the original value.
"""
