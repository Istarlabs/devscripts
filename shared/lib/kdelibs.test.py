#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks for kdelibs.py: python3 kdelibs.test.py"""

import importlib.util
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("kdelibs", Path(__file__).with_name("kdelibs.py"))
kdelibs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kdelibs)

with tempfile.TemporaryDirectory() as temporary:
    lib = Path(temporary) / "lib64"
    (lib / "cmake" / "KF6Config").mkdir(parents=True)
    (lib / "cmake" / "Qt6Core").mkdir(parents=True)
    for name in ("libKF6ConfigCore.so.6", "libKF6Holidays.so.6", "libKPim6Mime.so.6", "libQt6Core.so.6"):
        (lib / name).touch()

    packages = kdelibs.cmake_packages([lib])
    assert list(packages) == ["KF6Config"], packages

    # ConfigCore is covered by the KF6Config package; Holidays and Mime have no -devel installed.
    assert list(kdelibs.runtime_libraries(packages, [lib])) == ["KF6Holidays", "KPim6Mime"]

    qml = Path(temporary) / "qml"
    for module in ("org/kde/kirigami", "org/kde/kirigamiaddons/formcard", "org/other", "org/kde/kirigami/private",
                   "org/kde/union/impl"):
        (qml / module).mkdir(parents=True)
        (qml / module / "qmldir").touch()

    assert kdelibs.qml_modules(qml) == ["org.kde.kirigami", "org.kde.kirigamiaddons.formcard"]
    assert kdelibs.qml_modules(Path(temporary) / "missing") == []

used = kdelibs.used_packages("""
find_package(ECM ${KF_MIN_VERSION} REQUIRED NO_MODULE)
find_package(KF6 ${KF_MIN_VERSION} REQUIRED COMPONENTS
    I18n
    Config
)
find_package(KF6KirigamiAddons REQUIRED)
""")
assert used == {"ECM", "KF6I18n", "KF6Config", "KF6KirigamiAddons"}, used

assert kdelibs.used_qml_modules("import QtQuick\nimport org.kde.kirigami as Kirigami\n  import org.kde.config\n") \
    == {"org.kde.kirigami", "org.kde.config"}

print("kdelibs: all checks passed")
