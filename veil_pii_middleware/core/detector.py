"""Hybrid detection: deterministic regex layer unioned with a local LLM layer.

Regex covers email, DNI/NIE, IBAN, Spanish phones, credit cards and IPs with
mandatory check-digit validation. The local model (via Ollama) covers person
names, addresses and indirect references, and must return spans as JSON --
never rewritten text. On overlap the longest span wins; ties go to regex.
"""
