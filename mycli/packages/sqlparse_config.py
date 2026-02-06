"""Centralized sqlparse configuration.

Set MAX_GROUPING_DEPTH and MAX_GROUPING_TOKENS once instead of
duplicating these lines across every module that imports sqlparse.
"""
import sqlparse

sqlparse.engine.grouping.MAX_GROUPING_DEPTH = None  # type: ignore[assignment]
sqlparse.engine.grouping.MAX_GROUPING_TOKENS = None  # type: ignore[assignment]
