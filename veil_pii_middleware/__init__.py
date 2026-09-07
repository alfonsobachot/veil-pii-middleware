"""PII sanitization middleware for Claude Code.

Tokenizes personal data before it leaves the machine and reverses the
substitution for local tools, so raw PII never reaches an external API.
"""

__version__ = "0.1.0"
