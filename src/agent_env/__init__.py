"""The agent's ENVIRONMENT — the tools it can call to see the book and act on it.

Spec: docs/superpowers/specs/2026-08-09-agent-authority-inversion-design.md §6.

This package exists to ENABLE judgment, not to constrain it. Every tool answers a
question the agent might have: what do I hold, how am I doing against the mandate,
what does the screen rank highest, how far does this name actually move. The one
restrictive tool (`check_order`) is here so the agent can ASK whether something is
permitted before trying it.
"""
