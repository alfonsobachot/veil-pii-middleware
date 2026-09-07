"""Generates the fake SaaS billing database and its ground-truth manifest.

Faker with the ``es_ES`` locale and a fixed seed, valid check digits, and PII
deliberately scattered where column-level masking cannot reach. Emits
``demo/manifest.json`` alongside the SQLite database.
"""
