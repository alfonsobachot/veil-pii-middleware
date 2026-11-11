"""HTTP proxy at ``ANTHROPIC_BASE_URL`` -- full, fail-closed coverage.

See ``server.py`` for the implementation: the request's ``messages`` are
tokenized and the response's text is detokenized on the way back. Run as
``python -m veil_pii_middleware.adapters.gateway.server [port]``.
"""
