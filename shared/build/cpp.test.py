#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks what `cpp.py uninstall` may delete. Run after touching it:

    python3 ~/dev/tools/devscripts/shared/build/cpp.test.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(os.path.dirname(HERE))

# Importing the module looks up its project; this checkout carries its own Scripts/project.toml.
os.environ.setdefault("DEVSCRIPTS_PROJECT", CHECKOUT)
sys.path[:0] = [CHECKOUT, *(entry.path for entry in os.scandir(os.path.dirname(HERE)) if entry.is_dir())]

spec = importlib.util.spec_from_file_location("cpp", os.path.join(HERE, "cpp.py"))
cpp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpp)

manifest = "/home/u/.local/bin/app\n\n/home/u/.local/share/applications/app.desktop\n/usr/bin/app\n/home/u/.localx/evil\n"
# Only what lies under the install root: a manifest from a /usr build, or a sibling directory whose name merely
# starts the same, is not removed.
assert cpp.manifest_targets(manifest, "/home/u/.local") == [
    "/home/u/.local/bin/app",
    "/home/u/.local/share/applications/app.desktop",
], cpp.manifest_targets(manifest, "/home/u/.local")

print("ok")
