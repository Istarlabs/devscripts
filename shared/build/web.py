#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""npm chores for a JavaScript / TypeScript / Vue part of a project.

    python3 Scripts/web.py install     npm ci (npm install without a lockfile)
    python3 Scripts/web.py lint        npm run lint
    python3 Scripts/web.py typecheck   npm run typecheck   (tsc / vue-tsc --noEmit)
    python3 Scripts/web.py test        npm run test
    python3 Scripts/web.py build       npm run build
    python3 Scripts/web.py verify      install if node_modules is missing, then every one of the above the
                                       package.json defines - a missing script is skipped and said so

The tools themselves (eslint, tsc, vue-tsc, vitest, vite, esbuild) are the project's own devDependencies,
reached through its package.json scripts; nothing here knows which. [web] directory (default ".") is the
folder holding package.json.
"""
import argparse
import json
import subprocess
import sys

from project import project

DIRECTORY = project.path("web.directory", ".")
STEPS = ["lint", "typecheck", "test", "build"]


def npm(*arguments):
    print("+ npm " + " ".join(arguments), flush=True)

    return subprocess.run(["npm", *arguments], cwd=DIRECTORY).returncode


def scripts():
    manifest = DIRECTORY / "package.json"

    if not manifest.is_file():
        sys.exit(f"refused: {manifest} does not exist - set [web] directory in {project.config_file.name}.")

    return json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {})


def install():
    lockfile = (DIRECTORY / "package-lock.json").is_file()

    return npm("ci" if lockfile else "install")


def run_step(name, defined):
    if name not in defined:
        print(f"- {name}: no \"{name}\" script in package.json, skipped")

        return 0

    return npm("run", name)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["install", "verify", *STEPS])
    command = parser.parse_args().command

    if command == "install":
        sys.exit(install())

    defined = scripts()

    if command != "verify":
        sys.exit(run_step(command, defined))

    if not (DIRECTORY / "node_modules").is_dir() and install() != 0:
        sys.exit(1)

    failed = [name for name in STEPS if run_step(name, defined) != 0]

    if failed:
        sys.exit(f"verify failed: {', '.join(failed)}")

    print("verify ok")


if __name__ == "__main__":
    main()
