"""Gör `python -m ica_mcp` likvärdigt med konsolskriptet `ica-mcp`."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
