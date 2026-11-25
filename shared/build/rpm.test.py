#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks the pure parts of rpm.py: versions, %files, the spec. Run after touching them:

    python3 ~/dev/tools/devscripts/shared/build/rpm.test.py
"""

import datetime
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(os.path.dirname(HERE))

# Importing the module looks up its project; this checkout carries its own Scripts/project.toml.
os.environ.setdefault("DEVSCRIPTS_PROJECT", CHECKOUT)
sys.path[:0] = [CHECKOUT, *(entry.path for entry in os.scandir(os.path.dirname(HERE)) if entry.is_dir())]

spec = importlib.util.spec_from_file_location("rpm", os.path.join(HERE, "rpm.py"))
rpm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpm)

assert rpm.read_version('[package]\nname = "x"\nversion = "0.1.0"\n', "Cargo.toml") == "0.1.0"
assert rpm.read_version('[workspace.package]\nversion = "2.0.0"\n', "Cargo.toml") == "2.0.0"
assert rpm.read_version('[package]\nversion.workspace = true\n', "Cargo.toml") is None
assert rpm.read_version("project(gputaskman-qt VERSION 0.3.1 LANGUAGES CXX)", "CMakeLists.txt") == "0.3.1"
assert rpm.read_version("project(x LANGUAGES CXX)", "CMakeLists.txt") is None
assert rpm.read_version("<PropertyGroup><Version>1.4.0</Version></PropertyGroup>", "App.csproj") == "1.4.0"

# The git height of a CMake VERSION x.y: commits since the version line last changed, 0 on that commit.
with tempfile.TemporaryDirectory() as scratch:
    folder = Path(scratch)
    cmake = folder / "CMakeLists.txt"

    def commit(message):
        subprocess.run(["git", "-C", scratch, "add", "-A"], check=True)
        subprocess.run(["git", "-C", scratch, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message],
                       check=True)

    assert rpm.with_height("0.1", cmake) == "0.1.0", "no repository at all is height 0"
    subprocess.run(["git", "init", "-q", scratch], check=True)
    cmake.write_text("cmake_minimum_required(VERSION 3.20)\nproject(x VERSION 0.1 LANGUAGES CXX)\n")
    commit("start")
    assert rpm.with_height("0.1", cmake) == "0.1.0"
    cmake.write_text(cmake.read_text().replace("3.20", "3.25"))  # cmake_minimum_required's VERSION is not ours
    commit("minimum")
    (folder / "a.cpp").write_text("x")
    commit("a")
    cmake.write_text(cmake.read_text() + "add_subdirectory(src)\n")  # another line of the file is no new version
    commit("b")
    assert rpm.with_height("0.1", cmake) == "0.1.3"
    cmake.write_text(cmake.read_text().replace("VERSION 0.1", "VERSION 0.2"))
    commit("bump")
    assert rpm.with_height("0.2", cmake) == "0.2.0"
    assert rpm.with_height("0.2.5", cmake) == "0.2.5", "three numbers are left alone"
    assert rpm.with_height("1.0", folder / "Cargo.toml") == "1.0"

assert rpm.release_stamp(datetime.datetime(2026, 11, 14, 9, 5)) == "202611140905"

with tempfile.TemporaryDirectory() as scratch:
    stage = Path(scratch)
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/bin/app").write_text("x")
    (stage / "usr/lib/app/sub").mkdir(parents=True)
    (stage / "usr/lib/app/sub/file").write_text("x")
    (stage / "usr/share/applications").mkdir(parents=True)
    (stage / "usr/share/applications/app.desktop").write_text("x")
    (stage / "usr/bin/app-link").symlink_to("/usr/lib/app/sub/file")
    paths, directories = rpm.package_paths(stage, "app")
    # The package's own folder is owned as a whole; /usr/bin and /usr/share/applications (which merely starts
    # with the name) are not claimed.
    assert directories == ["/usr/lib/app"], directories
    assert paths == ["/usr/bin/app", "/usr/bin/app-link", "/usr/share/applications/app.desktop"], paths

    text = rpm.spec_text({"name": "app", "version": "1.0", "release": "202611140905", "summary": "An app",
                          "license": "MIT", "requires": ["dotnet-runtime-10.0"]},
                         stage, ["/usr/bin/a 100%"], ["/usr/lib/app"])
    lines = text.splitlines()
    assert "Requires: dotnet-runtime-10.0" in lines
    assert '"/usr/bin/a 100%%"' in lines  # quoted, and % escaped for the spec
    assert not any(line.startswith(("URL:", "BuildArch:")) for line in lines)
    assert lines[lines.index("%description") + 1] == "An app"

    scripted = rpm.spec_text({"name": "app", "version": "1.0", "release": "1", "summary": "s", "license": "MIT",
                              "recommends": ["extra"], "post": "udevadm trigger || :\n",
                              "preun": "if [ $1 -eq 0 ]; then x; fi"},
                             stage, ["/usr/bin/app"], [])
    lines = scripted.splitlines()
    assert "Recommends: extra" in lines
    assert lines[lines.index("%post") + 1] == "udevadm trigger || :"
    assert lines[lines.index("%preun") + 1] == "if [ $1 -eq 0 ]; then x; fi"
    assert "%postun" not in lines
    assert lines.index("%post") < lines.index("%files")

# The fingerprint: the same tree and spec give the same one, whatever the release and the staging folder;
# a byte, a mode, a link target or a line of the spec changes it.
meta = {"name": "app", "version": "1.0", "release": "202611140905", "summary": "s", "license": "MIT"}
with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
    stages = [Path(first), Path(second)]
    for stage in stages:
        (stage / "usr/bin").mkdir(parents=True)
        (stage / "usr/bin/app").write_bytes(b"\x7fELF binary")
        (stage / "usr/bin/app-link").symlink_to("app")

    def mark(stage, **changes):
        return rpm.fingerprint(stage, {**meta, **changes}, ["/usr/bin/app", "/usr/bin/app-link"], [])

    base = mark(stages[0])
    assert base == mark(stages[1]), "the staging folder's name must not count"
    assert base == mark(stages[0], release="209901010000"), "the release must not count"
    assert base != mark(stages[0], version="1.1")
    assert base != mark(stages[0], post="echo x")

    (stages[1] / "usr/bin/app").write_bytes(b"\x7fELF binarY")
    assert mark(stages[1]) != base
    (stages[1] / "usr/bin/app").write_bytes(b"\x7fELF binary")
    assert mark(stages[1]) == base
    (stages[0] / "usr/bin/app").chmod(0o644)
    (stages[1] / "usr/bin/app").chmod(0o755)
    assert mark(stages[1]) != mark(stages[0])
    (stages[1] / "usr/bin/app").chmod(0o644)
    base = mark(stages[0])
    assert mark(stages[1]) == base
    (stages[1] / "usr/bin/app-link").unlink()
    (stages[1] / "usr/bin/app-link").symlink_to("other")
    assert mark(stages[1]) != base

# One package: [rpm] itself. Several: one per [rpm.packages.NAME], the shared keys of [rpm] underneath.
assert rpm.package_configs({"name": "a", "license": "MIT"}) == [{"name": "a", "license": "MIT"}]
assert rpm.package_configs({}) == []
several = rpm.package_configs({"license": "GPL", "version_file": "CMakeLists.txt",
                               "packages": {"app": {"summary": "the app"},
                                            "app-plugin": {"summary": "the plugin", "license": "MIT"}}})
assert several == [
    {"license": "GPL", "version_file": "CMakeLists.txt", "summary": "the app", "name": "app"},
    {"license": "MIT", "version_file": "CMakeLists.txt", "summary": "the plugin", "name": "app-plugin"},
], several

print("ok")
