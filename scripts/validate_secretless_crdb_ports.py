#!/usr/bin/env python3
"""Fail closed when the Secretless CockroachDB port contract drifts."""

from __future__ import annotations

import sys

from secretless_crdb_contract import main


if __name__ == "__main__":
    sys.argv.append("--validate")
    raise SystemExit(main())
