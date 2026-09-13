"""Atomic env-file merge helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def merge_env_file(path: Path, values: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                lines.append(line)
                continue
            key = line[: line.index("=")]
            if key in values:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sprout-env-")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def read_env_values(path: Path, keys: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line[: line.index("=")]
        if key in keys:
            out[key] = line[line.index("=") + 1 :]
    return out
