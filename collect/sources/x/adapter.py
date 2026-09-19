# -*- coding: utf-8 -*-
"""Unified collect interface implementation for X."""
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass
class CollectResult:
    source: str
    items: list[dict] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0
    failed: int = 0
    rate_limited: bool = False
    error: str = ""

class XAdapter:
    name = "x"
    platform = "x"

    async def collect(self, log: Callable[[str], None] = print, state: Any = None) -> CollectResult:
        """Run the stable X collector and wrap its result in the common contract."""
        from .scan import run_scan
        try:
            items = await run_scan(log)
            return CollectResult(source=self.name, items=items, scanned=len(items))
        except Exception as exc:
            log(f"[源失败] {self.name}: {exc}")
            return CollectResult(source=self.name, failed=1, error=str(exc))
