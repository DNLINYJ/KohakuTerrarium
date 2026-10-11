"""Locate the kt-biome bundle that catalog workflows install into their isolated config."""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def real_home() -> Path:
    """The operator's home, captured by ``tests/conftest.py`` before HOME is redirected."""
    return Path(os.environ.get("KT_TEST_REAL_HOME") or Path.home())


def find_kt_biome_source() -> Path | None:
    """Return a kt-biome source directory, or None when none is available.

    Dev machines carry a repo-local ``kt-biome/`` checkout; CI installs kt-biome
    editable into the operator config dir before the suite runs
    (``.github/workflows/ci.yml``), leaving ``kt-biome.link`` pointing at the clone.
    Both are read only.
    """
    repo_local = _REPO_ROOT / "kt-biome"
    if (repo_local / "kohaku.yaml").exists():
        return repo_local
    real_pkgs = real_home() / ".kohakuterrarium" / "packages"
    link = real_pkgs / "kt-biome.link"
    if link.exists():
        target = Path(link.read_text(encoding="utf-8").strip())
        if (target / "kohaku.yaml").exists():
            return target
    direct = real_pkgs / "kt-biome"
    if (direct / "kohaku.yaml").exists():
        return direct
    return None
