#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks the version rules of `dotnet.py publish`. Run after touching them:

    python3 ~/dev/tools/devscripts/shared/build/dotnet.test.py

It lives beside dotnet.py rather than in the project that publishes a package: the rules are the
script's, and a copy in one project would only be read when that project happens to be open.
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(os.path.dirname(HERE))

# The functions under test are pure, but importing the module runs its config lookups, so it needs a
# project to belong to. This checkout is one - it carries its own Scripts/project.toml.
os.environ.setdefault("DEVSCRIPTS_PROJECT", CHECKOUT)
sys.path[:0] = [CHECKOUT, *(entry.path for entry in os.scandir(os.path.dirname(HERE)) if entry.is_dir())]

spec = importlib.util.spec_from_file_location("dotnet", os.path.join(HERE, "dotnet.py"))
dotnet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dotnet)

# Precedence: numbers as numbers, a release above its prereleases.
ordered = ["1.0.0", "1.1.0-preview.2", "1.1.0-preview.10", "1.1.0-rc.1", "1.1.0", "1.1.1", "1.10.0", "2.0.0"]
assert sorted(reversed(ordered), key=dotnet.version_key) == ordered, sorted(ordered, key=dotnet.version_key)

cases = [
    # (on the feed, in the .csproj, expected next)
    ([], "1.0.0", "1.0.0"),
    (["1.1.0-preview.1", "1.1.0-preview.2"], "1.0.0", "1.1.0-preview.3"),
    (["1.1.0-preview.9"], "1.0.0", "1.1.0-preview.10"),
    (["1.1.0-preview.2"], "1.1.0", "1.1.0"),
    (["1.1.0"], "1.1.0", "1.1.1"),
    (["1.1.0", "1.1.0-preview.2"], "1.0.0", "1.1.1"),
    (["1.1.4"], "1.2.0", "1.2.0"),
    (["1.1.4"], "2.0.0-rc.1", "2.0.0-rc.1"),
    (["1.1.0-preview"], "1.0.0", "1.1.1-preview"),
]

for published, declared, expected in cases:
    actual = dotnet.next_version(published, declared)
    assert actual == expected, f"{published} + {declared}: expected {expected}, got {actual}"

# The published version is written back into the project file, and nothing else in it changes.
with tempfile.TemporaryDirectory() as directory:
    project = os.path.join(directory, "Package.csproj")
    before = "<Project>\n  <PropertyGroup>\n    <Version>1.0.0</Version>\n    <FileVersion>1.0.0</FileVersion>\n"

    with open(project, "w", encoding="utf-8") as handle:
        handle.write(before)

    dotnet.write_version(project, "1.1.0-preview.3")

    with open(project, encoding="utf-8") as handle:
        assert handle.read() == before.replace("<Version>1.0.0<", "<Version>1.1.0-preview.3<")

# Several packages published together keep one <Version> in Directory.Build.props; that is where it is
# written back. A project that declares its own keeps it in its own file.
with tempfile.TemporaryDirectory() as directory:
    dotnet.REPO = directory
    props = os.path.join(directory, "Directory.Build.props")
    shared = os.path.join(directory, "Core", "Core.csproj")
    own = os.path.join(directory, "Own", "Own.csproj")
    os.makedirs(os.path.dirname(shared))
    os.makedirs(os.path.dirname(own))

    with open(shared, "w", encoding="utf-8") as handle:
        handle.write("<Project><PropertyGroup><PackageId>Core</PackageId></PropertyGroup></Project>\n")

    with open(own, "w", encoding="utf-8") as handle:
        handle.write("<Project><PropertyGroup><Version>2.0.0</Version></PropertyGroup></Project>\n")

    assert dotnet.version_file(shared) == shared, "no props: the project file itself"

    with open(props, "w", encoding="utf-8") as handle:
        handle.write("<Project><PropertyGroup><Version>1.0.0</Version></PropertyGroup></Project>\n")

    assert dotnet.version_file(shared) == props, dotnet.version_file(shared)
    assert dotnet.version_file(own) == own, dotnet.version_file(own)

# A restore error has no line number; a failed build must not read as a clean one.
restore_output = ("  Determining projects to restore...\n"
                  "/repo/A/A.csproj : error NU1605: Warning As Error: Detected package downgrade: X from 2 to 1.  [/repo/B/B.csproj]\n"
                  "  Failed to restore /repo/A/A.csproj (in 144 ms).\n")
found = dotnet.unparsed_errors(restore_output, "/repo/App.sln")
assert [code for _, _, code, _ in found] == ["NU1605"], found
assert found[0][0] == "/repo/A/A.csproj", found

silent = dotnet.unparsed_errors("  something went wrong\n", "/repo/App.sln")
assert silent == [("/repo/App.sln", 0, "BUILD", "something went wrong")], silent

# A schema is new when the db-init diff adds its CREATE line, plain or idempotent; a table, a comment
# or a removed line that mentions a schema is not one.
schema_diff = ("+CREATE SCHEMA IF NOT EXISTS nutrition;\n"
               "+CREATE SCHEMA audit;\n"
               "-CREATE SCHEMA dbo;\n"
               "+ALTER TABLE dbo.\"X\" SET SCHEMA files;\n"
               "+-- CREATE SCHEMA ignored;\n")
assert dotnet.new_schemas_in(schema_diff) == ["nutrition", "audit"], dotnet.new_schemas_in(schema_diff)
assert dotnet.new_schemas_in("") == []

# `packages --set` rewrites the version of exactly the named package, in every project and props file, and
# leaves build output alone. A package id that is the start of another one (EntityFrameworkCore, ...Design) is a
# different package, and an id nothing references is an error, not a silent skip.
with tempfile.TemporaryDirectory() as directory:
    dotnet.REPO = directory

    def project_file(name, text):
        full = os.path.join(directory, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)

        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

        return full

    web = project_file("Web/Web.csproj",
                       '<Project><ItemGroup>\n'
                       '<PackageReference Include="Microsoft.EntityFrameworkCore" Version="11.0.0-rc.3" />\n'
                       '<PackageReference Include="Microsoft.EntityFrameworkCore.Design" Version="11.0.0-rc.3">\n'
                       '<PrivateAssets>all</PrivateAssets></PackageReference>\n'
                       '</ItemGroup></Project>\n')
    props = project_file("Directory.Packages.props",
                         '<Project><ItemGroup>'
                         '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.0-rc.3"/>'
                         '</ItemGroup></Project>\n')
    built = project_file("Web/obj/Web.csproj",
                         '<Project><PackageReference Include="Microsoft.EntityFrameworkCore" Version="11.0.0-rc.3"/></Project>\n')

    assert dotnet.set_package_versions(["Microsoft.EntityFrameworkCore=11.0.0"]) == 0

    with open(web, encoding="utf-8") as handle:
        text = handle.read()

    assert 'Include="Microsoft.EntityFrameworkCore" Version="11.0.0"' in text, text
    assert 'Include="Microsoft.EntityFrameworkCore.Design" Version="11.0.0-rc.3"' in text, text

    with open(props, encoding="utf-8") as handle:
        assert 'Version="11.0.0"/>' in handle.read()

    with open(built, encoding="utf-8") as handle:
        assert "11.0.0-rc.3" in handle.read(), "obj/ is not touched"

    assert dotnet.set_package_versions(["Nothing.Like.This=1.0.0"]) == 1
    assert dotnet.set_package_versions(["NoVersion"]) == 2

# A raw FluentDataGrid is counted by its opening tag; the localized grid, a row of a grid, build output and code-behind
# are not. bin/ and obj/ hold a second copy of everything.
with tempfile.TemporaryDirectory() as directory:
    def page(name, text):
        full = os.path.join(directory, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)

        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    page("A/Raw.razor", "<p>x</p>\n<FluentDataGrid TGridItem=\"Row\">\n</FluentDataGrid>\n")
    page("A/Bare.razor", "<FluentDataGrid>\n</FluentDataGrid>\n")
    page("A/Inline.razor", "<div><FluentDataGrid Items=\"@a\"/></div>\n")
    page("A/Localized.razor", "<LocalizedDataGrid TGridItem=\"Row\">\n<FluentDataGridRow/>\n</LocalizedDataGrid>\n")
    page("A/Code.razor.cs", "private FluentDataGrid<Row>? _grid; // <FluentDataGrid x\n")
    page("A/obj/Raw.razor", "<FluentDataGrid TGridItem=\"Row\">\n")

    found = sorted(dotnet.raw_data_grids(directory))
    assert found == [(os.path.join("A", "Bare.razor"), 1), (os.path.join("A", "Inline.razor"), 1),
                     (os.path.join("A", "Raw.razor"), 2)], found

# [nuget] is the official feed, [nuget.private] the one `--private` picks; nuget.org lists versions elsewhere.
class Config:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


real_project = dotnet.project
dotnet.project = Config({"nuget.feed": "https://api.nuget.org/v3/index.json",
                         "nuget.api_key_file": "~/.config/nuget-org-api-key",
                         "nuget.private.feed": "https://feed.example.invalid/v3/index.json"})
official = dotnet.feed_settings(False)
private = dotnet.feed_settings(True)
assert official[:3] == ("nuget", "https://api.nuget.org/v3/index.json", dotnet.NUGET_ORG_PACKAGES), official
assert official[3] == os.path.expanduser("~/.config/nuget-org-api-key"), official
assert private[:3] == ("nuget.private", "https://feed.example.invalid/v3/index.json",
                       "https://feed.example.invalid/v3/package/"), private
dotnet.project = Config({})
assert dotnet.feed_settings(True)[1] is None
dotnet.project = real_project

print(f"all {len(cases) + 8} checks pass")
