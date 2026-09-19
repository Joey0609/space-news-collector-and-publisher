# -*- coding: utf-8 -*-
"""X source adapter placeholder.

# This file is retained only as a source-registry note; implementation lives in sources/x/.
This module defines the future adapter boundary without changing the live collector yet.
"""
SOURCE = "x"
PLATFORM = "x"

async def fetch(log, state=None) -> list[dict]:
    """Return normalized RawItem dictionaries in the next P3 integration step."""
    raise NotImplementedError("X adapter integration is staged after the P1/P2 migration")
