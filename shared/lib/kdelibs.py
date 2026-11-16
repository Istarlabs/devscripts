# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""What KDE libraries this machine has, for `cpp.py kde`: the question "is there a framework for this already?"
answered from the disk rather than from memory.

Three kinds of answer, because they need different things before a project can use them:

- **dev**: the CMake package is there, `find_package` works now.
- **runtime**: only the library is installed; using it from C++ needs its `-devel` package first.
- **QML modules** under org.kde: usable from QML straight away, with no `-devel` package at all.

Packages are KDE Frameworks (KF6*), KDE PIM (KPim6*), Plasma, and the Wayland helpers KDE ships (LayerShellQt,
KWayland). A short description comes from the RPM database when there is one.
"""
import re
import shutil
import subprocess
from pathlib import Path

LIBRARY_DIRS = [Path("/usr/lib64"), Path("/usr/lib"), Path("/usr/lib/x86_64-linux-gnu")]

PACKAGE = re.compile(r"^(KF6|KPim6|Plasma|LayerShellQt|KWayland)\w*$")
LIBRARY = re.compile(r"^lib((?:KF6|KPim6|Plasma|LayerShellQt|KWayland)\w*)\.so\.\d+$")
FIND_PACKAGE = re.compile(r"find_package\s*\(\s*(\w+)([^)]*)\)", re.IGNORECASE)
QML_IMPORT = re.compile(r"^\s*import\s+(org\.kde\.[\w.]+)", re.MULTILINE)
INTERNAL_QML = re.compile(r"\.(private|impl)(\.|$)")
CMAKE_KEYWORDS = {"REQUIRED", "COMPONENTS", "OPTIONAL_COMPONENTS", "CONFIG", "NO_MODULE", "QUIET", "EXACT",
                  "MODULE", "GLOBAL", "NO_POLICY_SCOPE"}


def cmake_packages(library_dirs=LIBRARY_DIRS):
    """{package name: its cmake directory} for every KDE package find_package can find."""
    found = {}

    for directory in library_dirs:
        for entry in sorted((directory / "cmake").glob("*")) if (directory / "cmake").is_dir() else []:
            if entry.is_dir() and PACKAGE.match(entry.name):
                found.setdefault(entry.name, entry)

    return found


def runtime_libraries(packages, library_dirs=LIBRARY_DIRS):
    """{library name: its file} for KDE libraries that no CMake package covers - installed without -devel.

    A package covers the libraries named after it: KF6Config covers libKF6ConfigCore and libKF6ConfigGui.
    """
    found = {}

    for directory in library_dirs:
        for entry in sorted(directory.glob("lib*.so.*")) if directory.is_dir() else []:
            match = LIBRARY.match(entry.name)

            if match and not any(match[1].startswith(package) for package in packages):
                found.setdefault(match[1], entry)

    return found


def qml_modules(qml_dir):
    """The public org.kde.* QML module URIs installed under `qml_dir`; private and impl modules are no API."""
    root = Path(qml_dir) / "org" / "kde"

    if not root.is_dir():
        return []

    uris = (".".join(qmldir.parent.relative_to(qml_dir).parts) for qmldir in root.rglob("qmldir"))
    return sorted(uri for uri in uris if not INTERNAL_QML.search(uri))


def used_qml_modules(qml_text):
    """The org.kde modules a QML file imports."""
    return set(QML_IMPORT.findall(qml_text))


def used_packages(cmake_text):
    """The package names a CMakeLists.txt asks for; `find_package(KF6 COMPONENTS Config)` counts as KF6Config."""
    used = set()

    for name, rest in FIND_PACKAGE.findall(cmake_text):
        if name in ("KF6", "KPim6", "Plasma") and "COMPONENTS" in rest.upper():
            words = rest.split()
            components = words[words.index("COMPONENTS") + 1:] if "COMPONENTS" in words else []
            used.update(name + word for word in components
                        if word not in CMAKE_KEYWORDS and not word.startswith("$") and re.match(r"^\w+$", word))
        else:
            used.add(name)

    return used


def qml_dir():
    """Qt's QML import directory, asked of qtpaths when it is there."""
    for tool in ("qtpaths6", "qtpaths-qt6", "qtpaths"):
        if shutil.which(tool):
            result = subprocess.run([tool, "--query", "QT_INSTALL_QML"], capture_output=True, text=True)

            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

    return next((str(d / "qt6" / "qml") for d in LIBRARY_DIRS if (d / "qt6" / "qml").is_dir()), "")


def rpm_summaries(paths):
    """{path: (package name, summary of the library package)}; empty without rpm. Two calls, not one per path."""
    if not paths or not shutil.which("rpm"):
        return {}

    owners = subprocess.run(["rpm", "-qf", "--qf", "%{NAME}\n", *map(str, paths)], capture_output=True, text=True)
    names = owners.stdout.splitlines()

    if len(names) != len(paths):
        return {}

    # A cmake directory belongs to the -devel package, whose summary only says "development files".
    bases = sorted({name.removesuffix("-devel") for name in names})
    result = subprocess.run(["rpm", "-q", "--qf", "%{NAME}\t%{SUMMARY}\n", *bases], capture_output=True, text=True)
    summaries = dict(line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line)

    return {path: (name, summaries.get(name.removesuffix("-devel"), "")) for path, name in zip(paths, names)}


def report(project_root):
    """The listing `cpp.py kde` prints."""
    packages = cmake_packages()
    runtime = runtime_libraries(packages)
    modules = qml_modules(qml_dir())
    used = set()
    used_qml = set()

    for pattern, collect, into in (("CMakeLists.txt", used_packages, used), ("*.qml", used_qml_modules, used_qml)):
        for file in Path(project_root).rglob(pattern):
            if file.relative_to(project_root).parts[0] not in ("build", ".git"):
                into |= collect(file.read_text(encoding="utf-8", errors="replace"))

    info = rpm_summaries([*packages.values(), *runtime.values()])
    lines = ["dev = find_package works   runtime = library only, needs its -devel package   * = used here", ""]
    width = max((len(name) for name in [*packages, *runtime]), default=10)

    for name, path in sorted(packages.items()):
        summary = info.get(path, ("", ""))[1]
        lines.append(f"  {name:<{width}}  dev      {'*' if name in used else ' '}  {summary}".rstrip())

    for name, path in sorted(runtime.items()):
        package, summary = info.get(path, ("", ""))
        hint = f" (dnf install {package}-devel)" if package else ""
        lines.append(f"  {name:<{width}}  runtime     {summary}{hint}".rstrip())

    lines += ["", f"QML modules ({len(modules)} public, usable from QML without a -devel package):"]
    lines += [f"  {'*' if module in used_qml else ' '} {module}" for module in modules]

    return "\n".join(lines)
