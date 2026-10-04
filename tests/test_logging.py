"""Loggning: serve() ska ge 'NIVÅ namn: meddelande' på stderr, även fast
FastMCP redan har konfigurerat root-loggern vid import."""

import logging

from ica_mcp import server


def test_configure_logging_overrides_fastmcp_format(capsys):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        server._configure_logging()
        logging.getLogger("ica_mcp").info("hej")
        captured = capsys.readouterr()
        assert "INFO ica_mcp: hej" in captured.err
        assert captured.out == ""  # stdout är MCP-protokollets
    finally:
        root.handlers[:], root.level = saved
