import os
import sys
from pathlib import Path

import _tkinter


python_root = Path(sys.base_prefix)
tcl_root = python_root / "tcl"
tcl_version = _tkinter.TCL_VERSION
tk_version = _tkinter.TK_VERSION

tcl_data = Path(os.environ.get("TCL_LIBRARY", tcl_root / f"tcl{tcl_version}"))
tk_data = Path(os.environ.get("TK_LIBRARY", tcl_root / f"tk{tk_version}"))
tcl_modules = tcl_root / f"tcl{tcl_version.split('.')[0]}"

datas = []
for source, destination in (
    (tcl_data, "_tcl_data"),
    (tk_data, "_tk_data"),
    (tcl_modules, tcl_modules.name),
):
    if source.is_dir():
        datas.append((str(source), destination))

binaries = []
for name in ("tcl86t.dll", "tk86t.dll"):
    source = python_root / "DLLs" / name
    if source.is_file():
        binaries.append((str(source), "."))
