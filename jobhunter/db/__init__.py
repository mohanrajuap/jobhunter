"""Optional external database sinks. SQLite remains the source of truth."""

from .oracle_sink import OracleSink, build_sink

__all__ = ["OracleSink", "build_sink"]
