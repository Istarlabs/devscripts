#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-check for browser_extension.py: `python3 browser_extension.test.py`."""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

root = pathlib.Path(tempfile.mkdtemp())
(root / "Scripts").mkdir()
(root / "Scripts" / "project.toml").write_text('name = "Demo"\n[browser_extension]\ndirectory = "Ext"\n', encoding="utf-8")
(root / "Ext" / "web-ext-artifacts").mkdir(parents=True)
(root / "Ext" / "manifest.json").write_text('{\n  "manifest_version": 3,\n  "name": "Demo",\n  "version": "1.9"\n}\n', encoding="utf-8")

for version in ("1.2", "1.10", "1.9"):
    (root / "Ext" / "web-ext-artifacts" / f"abc-{version}.xpi").write_bytes(b"")

os.environ["DEVSCRIPTS_PROJECT"] = str(root)
sys.path[:0] = [str(folder) for folder in pathlib.Path(__file__).resolve().parents[1].iterdir() if folder.is_dir()] + [str(pathlib.Path(__file__).resolve().parents[2])]
spec = importlib.util.spec_from_file_location("browser_extension", pathlib.Path(__file__).with_name("browser_extension.py"))
extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extension)

assert extension.next_version("1.9") == "1.10"
assert extension.signed_versions() == ["1.2", "1.9", "1.10"], extension.signed_versions()


def refuses(function, *arguments):
    try:
        function(*arguments)
    except SystemExit as error:
        return str(error.code)

    raise AssertionError("did not refuse")


assert "already has a signed" in refuses(extension.sign, SimpleNamespace(listed=False))

extension.bump(SimpleNamespace(version=None))
manifest = json.loads((root / "Ext" / "manifest.json").read_text(encoding="utf-8"))
assert manifest["version"] == "1.10" and manifest["manifest_version"] == 3, manifest
assert "not higher" in refuses(extension.bump, SimpleNamespace(version="1.2"))

extension.bump(SimpleNamespace(version="2.0"))
assert "already has" not in str(extension.signed_versions()) and extension.read_manifest()["version"] == "2.0"

os.environ.pop(extension.KEY_VARIABLE, None)
assert extension.KEY_VARIABLE in refuses(extension.sign, SimpleNamespace(listed=False))
folders, file_name = extension.upload_target("2.0")
assert folders == ["Internal Programs", root.name, "Ext"], folders
assert file_name == f"{root.name.lower()}-ext-2.0.xpi", file_name

assert "no signed" in refuses(extension.upload, SimpleNamespace())
(root / "Ext" / "web-ext-artifacts" / "abc-2.0.xpi").write_bytes(b"")
os.environ.pop(extension.NEXTCLOUD_WALLET_KEY, None)
assert extension.NEXTCLOUD_WALLET_KEY in refuses(extension.upload, SimpleNamespace())
print("ok")
