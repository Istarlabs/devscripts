#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks what `cpp.py uninstall` may delete and how `cpp.py tidy`, `sanitize` and `fuzz` read their tools' output.
Run after touching either:

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

# What a sanitizer says, in a test's output (with ctest's "N: " in front when it is verbose) or a fuzzer's: the
# finding once, the stack below it left alone, and nothing for a line that only mentions the words.
sanitizer_output = """test 2
2: ==241==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x7c28347e0178 at pc 0x422d66
2:     #0 0x422d65 in qFromUnaligned /usr/include/qt6/QtCore/qendian.h:57
/p/src/siidump.cpp:62:38: runtime error: negation of -9223372036854775808 cannot be represented in type 'long long'
SUMMARY: AddressSanitizer: heap-buffer-overflow /usr/include/qt6/QtCore/qendian.h:57 in qFromUnaligned
==242==ERROR: libFuzzer: deadly signal
SUMMARY: AddressSanitizer: heap-buffer-overflow /usr/include/qt6/QtCore/qendian.h:57 in qFromUnaligned
terminate called after throwing an instance of 'std::out_of_range'
  what():  basic_string_view::at: __pos (which is 37) >= this->size() (which is 37)
PASS   : AddressSanitizerTest::notAFinding()
"""
assert cpp.sanitizer_reports(sanitizer_output) == [
    "==241==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x7c28347e0178 at pc 0x422d66",
    "/p/src/siidump.cpp:62:38: runtime error: negation of -9223372036854775808 cannot be represented in type 'long long'",
    "SUMMARY: AddressSanitizer: heap-buffer-overflow /usr/include/qt6/QtCore/qendian.h:57 in qFromUnaligned",
    "==242==ERROR: libFuzzer: deadly signal",
    "terminate called after throwing an instance of 'std::out_of_range'",
    "what():  basic_string_view::at: __pos (which is 37) >= this->size() (which is 37)",
], cpp.sanitizer_reports(sanitizer_output)
assert cpp.sanitizer_reports("PASS   : a()\nTotals: 3 passed\n") == []

# libFuzzer's closing line is the last "DONE" one; a run killed before it has none.
fuzz_output = """#2\tINITED cov: 12 ft: 14 corp: 2/20b exec/s: 0 rss: 40Mb
#1048576\tpulse  cov: 90 ft: 150 corp: 30/900b lim: 4096 exec/s: 349525 rss: 60Mb
#2750000\tDONE   cov: 95 ft: 160 corp: 33/1100b lim: 65536 exec/s: 91666 rss: 61Mb
"""
assert cpp.fuzz_summary(fuzz_output) == (2750000, 95, 160, 33), cpp.fuzz_summary(fuzz_output)
assert cpp.fuzz_summary("#2\tINITED cov: 12 ft: 14 corp: 2/20b exec/s: 0 rss: 40Mb\n") is None

print("ok")
