"""Local deployment layer for the Windows offline (self-contained) install.

Everything that turns "the code" into "a running installation on a machine with no
Python, no Node, no Docker" lives in this package:

  paths      install layout (program dir / pgsql / data dir / config / models)
  state      deployment.json — installed version, ports, upgrade history
  conf       .env generation + reading (secrets are generated once, at install)
  pg         bundled PostgreSQL lifecycle: initdb / configure / start / stop /
             create role+db / dump / restore (pure subprocess, no psql parsing)
  schema     idempotent schema reconciliation (create_all + ADD COLUMN) so an
             upgrade from an older version never loses data
  backup     pg_dump based backup / verify / restore / retention
  ai_engine  offline model registration (bundled GGUF files -> DB rows) + probe
  provision  the one command the installer calls: initdb + service + DB + schema
             + first admin + config + backup task
  doctor     self-check used by the installer's post-install test and by support
  cli        `AIMS-Backend.exe deploy <cmd>` entry point

The package is deliberately dependent on nothing but the standard library plus
psycopg2 (already an app dependency) so that `provision` works inside the frozen
EXE before the FastAPI app is ever imported.
"""
from __future__ import annotations

APP_VERSION = "1.0.1"

__all__ = ["APP_VERSION"]
