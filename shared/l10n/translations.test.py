#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks translations.py and gen_resx.py against a throwaway project with two satellite languages.

What is pinned here is the contract a translator relies on: a language may lack a key, a base text
changed without a translation leaves that translation outdated, a placeholder the base text does not
have is refused, and a satellite without a key falls back instead of failing the generator.

    python3 translations.test.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

CHECKOUT = pathlib.Path(__file__).resolve().parents[2]

FAILURES = []

SHIM = """import os, runpy, sys
SHARED = {checkout!r}
FOLDERS = [entry.path for entry in os.scandir(os.path.join(SHARED, "shared")) if entry.is_dir()]
TARGET = next(os.path.join(folder, os.path.basename(__file__)) for folder in FOLDERS
              if os.path.isfile(os.path.join(folder, os.path.basename(__file__))))
os.environ["DEVSCRIPTS_PROJECT"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [SHARED, *FOLDERS]
runpy.run_path(TARGET, run_name="__main__")
"""

RESX_HEADER = """<?xml version="1.0" encoding="utf-8"?>
<root>
    <resheader name="resmimetype">
        <value>text/microsoft-resx</value>
    </resheader>
</root>
"""


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       expected {expected!r}\n       got      {actual!r}")
        FAILURES.append(name)


def make_project(root):
    scripts = root / "Scripts"
    (scripts / "translations").mkdir(parents=True)
    (root / "Res").mkdir()

    (scripts / "project.toml").write_text(
        'name = "Sample"\n\n[localization]\nresources_project = "Res"\nnamespace = "Res"\n'
        'base_language = "en"\nsatellite_languages = ["de", "fr"]\n')

    for script in ("translations.py", "gen_resx.py"):
        (scripts / script).write_text(SHIM.format(checkout=str(CHECKOUT)))

    (scripts / "translations" / "Area.json").write_text("{}\n")
    (root / "Res" / "Area.resx").write_text(RESX_HEADER)
    (root / "Res" / "Area.Designer.cs").write_text("namespace Res {\n    class Area {\n    }\n}")


def run(root, *arguments, payload=None):
    return subprocess.run([sys.executable, str(root / "Scripts" / "translations.py"), *arguments],
                          input=None if payload is None else json.dumps(payload),
                          capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        make_project(root)

        added = run(root, "add", "Area", payload={"Greeting": {"en": "Hello {0}", "de": "Hallo {0}"}})
        check("add with a language missing is accepted", added.returncode, 0)

        status = run(root, "status")
        check("the missing language is counted", "fr         1 missing" in status.stdout, True)

        french = (root / "Res" / "Area.fr.resx").read_text()
        check("a satellite without the key leaves it out", "Greeting" in french, False)

        refused = run(root, "update", "Area", payload={"Greeting": {"fr": "Bonjour"}})
        check("a translation dropping a placeholder is refused", refused.returncode != 0, True)

        run(root, "update", "Area", payload={"Greeting": {"en": "Hi {0}"}})
        entry = json.loads((root / "Scripts" / "translations" / "Area.json").read_text())["Greeting"]
        check("update keeps the languages it is not given", entry.get("de"), "Hallo {0}")

        status = run(root, "status", "de", "--list")
        check("a changed base text leaves the translation outdated",
              "outdated  Area.Greeting" in status.stdout, True)

        run(root, "update", "Area", payload={"Greeting": {"de": "Hi {0}"}})
        status = run(root, "status", "de")
        check("a new translation is current again", "0 outdated" in status.stdout, True)

        run(root, "update", "Area", payload={"Greeting": {"en": "Hey {0}"}})
        run(root, "mark-current", "de")
        status = run(root, "status", "de")
        check("mark-current leaves an outdated translation alone", "1 outdated" in status.stdout, True)

        run(root, "mark-current", "de", "--overwrite")
        status = run(root, "status", "de")
        check("mark-current --overwrite resets it", "0 outdated" in status.stdout, True)

        run(root, "add", "Area", payload={"Farewell": {"en": "Bye"}})
        accessor = ('        /// <summary>\n        ///   Looks up a localized string similar to {text}.\n'
                    '        /// </summary>\n        public static string {key} {{\n            get {{\n'
                    '                return ResourceManager.GetString("{key}", resourceCulture);\n'
                    '            }}\n        }}\n        \n')
        (root / "Res" / "Area.Designer.cs").write_text(
            "namespace Res {\n    class Area {\n"
            + accessor.format(key="Greeting", text="Hey") + accessor.format(key="Farewell", text="Bye")
            + "    }\n}")
        run(root, "remove", "Area", "Greeting", "--force")
        designer = (root / "Res" / "Area.Designer.cs").read_text()
        check("remove takes the doc comment of the accessor along", "similar to Hey" in designer, False)
        check("remove leaves the other accessors with theirs",
              "similar to Bye" in designer and "public static string Farewell" in designer, True)
        run(root, "remove", "Area", "Farewell", "--force")
        check("removing the last accessor leaves a valid class",
              (root / "Res" / "Area.Designer.cs").read_text().count("{") ==
              (root / "Res" / "Area.Designer.cs").read_text().count("}"), True)

        run(root, "add", "Area", payload={"Greeting": {"en": "Hello {0}", "de": "Hallo {0}"}})
        run(root, "update", "Area", payload={"Greeting": {"de": "Hallo {0}"}})
        run(root, "remove", "Area", "Greeting", "--force")
        records =json.loads((root / "Scripts" / "translations" / "translated-from" / "de.json").read_text())
        check("remove drops the record", records, {})

    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        make_project(root)

        for leftover in ("Area.resx", "Area.Designer.cs"):
            (root / "Res" / leftover).unlink()

        (root / "Scripts" / "translations" / "Area.json").unlink()
        (root / "Res" / "Res.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk">\n</Project>\n')

        created = run(root, "new-area", "FirstResources")
        check("new-area creates the first area with no template to clone", created.returncode, 0)

        resx = (root / "Res" / "FirstResources.resx").read_text()
        check("the first area gets the shipped resx header",
              "<resheader name=\"writer\">" in resx and resx.endswith("</root>\n"), True)

    print()

    if FAILURES:
        print(f"{len(FAILURES)} failure(s)")
        sys.exit(1)

    print("all passed")


if __name__ == "__main__":
    main()
