"""Fail early when the frozen backend spec names modules absent from CI dependencies."""
import ast
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = ROOT / "build" / "pyinstaller_backend.spec"
tree = ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))

hiddenimports = None
for node in ast.walk(tree):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        continue
    if node.func.id != "Analysis":
        continue
    for keyword in node.keywords:
        if keyword.arg == "hiddenimports":
            hiddenimports = ast.literal_eval(keyword.value)
            break
    if hiddenimports is not None:
        break

if not isinstance(hiddenimports, list) or not hiddenimports:
    raise SystemExit(f"Could not read hiddenimports from {SPEC}")

missing = []
for module in hiddenimports:
    try:
        found = importlib.util.find_spec(module)
    except (ImportError, ModuleNotFoundError, ValueError):
        found = None
    if found is None:
        missing.append(module)

if missing:
    raise SystemExit(
        "PyInstaller hidden imports are missing from the installed dependencies: "
        + ", ".join(missing)
    )

print(f"All {len(hiddenimports)} PyInstaller hidden imports are available.")
