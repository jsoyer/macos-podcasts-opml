import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "macos_podcasts_opml",
    Path(__file__).parent / "macos-podcasts-opml.py",
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["macos_podcasts_opml"] = _module
_spec.loader.exec_module(_module)  # type: ignore[union-attr]
