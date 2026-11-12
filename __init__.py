"""testsift plugin — exact, local analysis of JUnit XML test reports.

Registers three read-only tools (``test_report_summary``, ``test_report_failures``,
``test_report_compare``). Pure standard library: no network, no credentials, no
background work, no self-update.
"""

from __future__ import annotations

from . import testsift as _ts


def register(ctx) -> None:
    """Called once by the plugin loader; register the three read-only report tools."""
    ctx.register_tool(name="test_report_summary", toolset="testsift",
                      schema=_ts.TEST_REPORT_SUMMARY, handler=_ts.test_report_summary)
    ctx.register_tool(name="test_report_failures", toolset="testsift",
                      schema=_ts.TEST_REPORT_FAILURES, handler=_ts.test_report_failures)
    ctx.register_tool(name="test_report_compare", toolset="testsift",
                      schema=_ts.TEST_REPORT_COMPARE, handler=_ts.test_report_compare)
