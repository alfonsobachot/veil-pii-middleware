"""HTTP proxy at ``ANTHROPIC_BASE_URL`` -- full, fail-closed coverage.

See ``server.py`` for the implementation and its documented scope cuts
(request-side tokenization only; the response is relayed untouched). Run as
``python -m veil_pii_middleware.adapters.gateway.server [port]``.
"""
