"""Import a ``module:attribute`` reference declared in a package's ``ui:`` slot."""

import importlib
import sys
from typing import Any

from kohakuterrarium.packages.resolve import ensure_package_importable


def import_package_ref(package: str, ref: str, *, reload: bool = False) -> Any:
    """Return the attribute ``ref`` names; ``reload`` re-imports a cached module."""
    module_name, _, attr = ref.partition(":")
    ensure_package_importable(package)
    module = sys.modules.get(module_name)
    if module is not None and reload:
        module = importlib.reload(module)
    elif module is None:
        module = importlib.import_module(module_name)
    return getattr(module, attr)
