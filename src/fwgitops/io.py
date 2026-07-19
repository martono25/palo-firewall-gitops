"""File I/O helpers: YAML reading and intent discovery.

Isolated here so the compiler/intent core stay dependency-free; PyYAML is only
needed at the edge (reading files). `*.example.*` files are documentation and
are never treated as live intent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

try:
    import yaml
except ImportError as e:  # pragma: no cover - environment guard
    raise ImportError(
        "PyYAML is required to read intent/config files. Install it: pip install pyyaml"
    ) from e


def read_yaml(path: Path) -> Any:
    """Parse a YAML file to a Python object."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def discover_intents(root: Path) -> List[Path]:
    """Find intent files under root, skipping `*.example.*` documentation."""
    out: List[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        for p in root.rglob(pattern):
            if ".example." in p.name:
                continue
            out.append(p)
    return sorted(out)
