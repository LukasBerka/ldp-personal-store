"""The load-bearing guard of the package split: the view engine is a standalone product
that speaks only the pure standard LDP + SPARQL 1.1 contract, so no ``ldp_view_engine``
module may import ``ldp_personal_store`` (the reference storage). Only the bundled
``ldp_pod`` composition root is allowed to depend on both.

The boundary is asserted two ways: statically (every ``ldp_view_engine`` source file's
imports are parsed and none names ``ldp_personal_store``) and dynamically (a fresh
subprocess imports the whole engine package and reports any ``ldp_personal_store`` module
that got loaded on its account).
"""

import ast
import subprocess
import sys
from pathlib import Path

import ldp_view_engine

_FORBIDDEN = "ldp_personal_store"


def _engine_source_files() -> list[Path]:
    root = Path(ldp_view_engine.__file__).parent
    return sorted(root.rglob("*.py"))


def test_engine_sources_do_not_import_storage() -> None:
    offenders: list[str] = []
    for path in _engine_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                if name == _FORBIDDEN or name.startswith(_FORBIDDEN + "."):
                    offenders.append(f"{path.name}: imports {name}")
    assert not offenders, "ldp_view_engine must not import ldp_personal_store:\n" + "\n".join(
        offenders
    )


def test_importing_engine_loads_no_storage_module() -> None:
    # Run in a clean subprocess so importing the whole engine package cannot be masked by
    # storage modules a sibling test already loaded, and cannot perturb this process.
    program = (
        "import importlib, pkgutil, sys\n"
        "pkg = importlib.import_module('ldp_view_engine')\n"
        "for m in pkgutil.iter_modules(pkg.__path__):\n"
        "    importlib.import_module('ldp_view_engine.' + m.name)\n"
        "leaked = [n for n in sys.modules if n == 'ldp_personal_store'"
        " or n.startswith('ldp_personal_store.')]\n"
        "print('\\n'.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    leaked = [line for line in result.stdout.splitlines() if line.strip()]
    assert not leaked, f"importing ldp_view_engine pulled in storage modules: {leaked}"
