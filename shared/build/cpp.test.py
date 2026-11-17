#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks what `cpp.py uninstall` may delete and how `cpp.py tidy` reads clang-tidy. Run after touching either:

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

# A header's finding comes once per file that includes it, and is counted once; a relative path is the build
# directory's; the check is taken off the text; the NOLINT counts of every file add up.
tidy_output = """[1/2][0.5s] /usr/bin/clang-tidy -p=build /p/src/a.cpp
/p/src/a.h:3:5: warning: use of a signed integer operand [bugprone-signed-bitwise]
/p/src/a.cpp:9:1: warning: do not use reinterpret_cast [cppcoreguidelines-pro-type-reinterpret-cast,-warnings-as-errors]
Suppressed 120 warnings (118 in non-user code, 2 NOLINT).
[2/2][0.4s] /usr/bin/clang-tidy -p=build /p/src/b.cpp
/p/src/a.h:3:5: warning: use of a signed integer operand [bugprone-signed-bitwise]
../src/b.cpp:4:2: warning: function 'f' is too complex [readability-function-cognitive-complexity]
Suppressed 99 warnings (98 in non-user code, 1 NOLINT).
"""
findings, nolint = cpp.tidy_findings(tidy_output, "/p/build")
assert findings == [
    ("/p/src/a.cpp", 9, "cppcoreguidelines-pro-type-reinterpret-cast", "do not use reinterpret_cast"),
    ("/p/src/a.h", 3, "bugprone-signed-bitwise", "use of a signed integer operand"),
    ("/p/src/b.cpp", 4, "readability-function-cognitive-complexity", "function 'f' is too complex"),
], findings
assert nolint == 3, nolint

print("ok")
