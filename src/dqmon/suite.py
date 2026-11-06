from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .checks import CHECK_TYPES, Check


@dataclass
class Suite:
    name: str
    source: dict
    checks: list[Check] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Suite":
        if "name" not in data or "checks" not in data:
            raise ValueError("suite needs 'name' and 'checks'")
        checks: list[Check] = []
        for i, spec in enumerate(data["checks"], start=1):
            spec = dict(spec)
            ctype = spec.pop("type", None)
            if ctype not in CHECK_TYPES:
                raise ValueError(f"check #{i}: unknown type {ctype!r}. Valid: {sorted(CHECK_TYPES)}")
            try:
                checks.append(CHECK_TYPES[ctype](**spec))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"check #{i} ({ctype}): {exc}") from exc
        names = [c.name for c in checks]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate check names (add a 'name'): {dupes}")
        return cls(data["name"], data.get("source", {}), checks)

    @property
    def tables(self) -> list[str]:
        seen: list[str] = []
        for c in self.checks:
            if c.type != "custom_sql" and c.table not in seen:
                seen.append(c.table)
        return seen


def load_suite(path: str | Path) -> Suite:
    return Suite.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
