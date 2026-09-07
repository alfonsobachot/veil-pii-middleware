"""Substitution and reversal of ``PII_<TYPE>_<NNNN>`` tokens.

Operates over plain text and nested structures (dicts, lists, JSON payloads),
replacing detected spans with tokens on the way out and restoring the original
values on the way back in.
"""
