#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lint, build and sign the browser extension of a project.

Every one of these used to be a hand-typed `npx web-ext ...` line with the API key spelled out on the
command line, and a version in manifest.json that had to be raised by hand or Mozilla refused the upload.

    python3 Scripts/browser_extension.py status           name, version, signed versions, credentials
    python3 Scripts/browser_extension.py lint             web-ext lint
    python3 Scripts/browser_extension.py build            an unsigned .zip into the artifacts folder
    python3 Scripts/browser_extension.py bump [VERSION]   next version in manifest.json (1.4 -> 1.5), or VERSION
    python3 Scripts/browser_extension.py sign             lint, then Mozilla signs it as an unlisted add-on
    python3 Scripts/browser_extension.py sign --listed    ... for the public directory instead
    python3 Scripts/browser_extension.py upload           the signed .xpi to Nextcloud (WebDAV), for the other machines

`upload` puts `{project}-ext-{version}.xpi` under `Internal Programs/{Project}/{BrowserExtension}/` (missing
folders are created, an existing file of that version is replaced). NXT_CLD_USR names the user and
NXT_CLD_KW_SEC_KEY the KDE Wallet key that holds its app password; [browser_extension] may set `nextcloud_url`
(else DEVSCRIPTS_NEXTCLOUD_URL), `nextcloud_folder` and `nextcloud_project`.

`sign` refuses a version that already has a signed .xpi in the artifacts folder (Mozilla would refuse the
upload anyway) and a missing credential, naming it. The credentials are AMO_JWT_ISSUER and AMO_JWT_SECRET
from the environment; they are handed to web-ext through its own environment variables, so they never
appear on a command line.

Firefox grants the host permissions of a Manifest V3 add-on only when asked, and a signed .xpi is installed
through Add-ons -> gear -> Install Add-on From File.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from project import project

DIRECTORY = project.path("browser_extension.directory", "BrowserExtension")
ARTIFACTS = DIRECTORY / project.get("browser_extension.artifacts", "web-ext-artifacts")
MANIFEST = DIRECTORY / "manifest.json"

KEY_VARIABLE = "AMO_JWT_ISSUER"
SECRET_VARIABLE = "AMO_JWT_SECRET"
NEXTCLOUD_USER = "NXT_CLD_USR"
NEXTCLOUD_WALLET_KEY = "NXT_CLD_KW_SEC_KEY"

# `"version"` with its quote in front never matches inside "manifest_version".
VERSION_PATTERN = re.compile(r'("version"\s*:\s*")([^"]+)(")')


def refuse(message):
    sys.exit(f"refused: {message}")


def read_manifest():
    if not MANIFEST.is_file():
        refuse(f"{MANIFEST} does not exist - set [browser_extension] directory in {project.config_file.name}.")

    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def next_version(version):
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def signed_versions():
    """The versions that have a signed .xpi in the artifacts folder, oldest first."""
    found = {match.group(1) for path in ARTIFACTS.glob("*.xpi") if (match := re.search(r"-([\d.]+)\.xpi$", path.name))}

    return sorted(found, key=version_key)


def missing_credentials():
    return [name for name in (KEY_VARIABLE, SECRET_VARIABLE) if not os.environ.get(name)]


def web_ext(*arguments, environment=None):
    if not shutil.which("npx"):
        refuse("npx is not installed - web-ext runs through it.")

    return subprocess.run(["npx", "--yes", "web-ext", *arguments], cwd=DIRECTORY, env=environment).returncode


def status(_arguments):
    manifest = read_manifest()
    version = manifest["version"]
    signed = signed_versions()
    missing = missing_credentials()

    print(f"{manifest['name']} {version}  ({manifest.get('browser_specific_settings', {}).get('gecko', {}).get('id', 'no gecko id')})")
    print(f"signed   {', '.join(signed) or 'none'}  ({ARTIFACTS.relative_to(project.root)})")
    print("this version is " + ("already signed - `bump` before signing" if version in signed else "not signed yet"))
    print("credentials " + ("set" if not missing else f"missing: {', '.join(missing)}"))

    return 0


def lint(_arguments):
    read_manifest()

    return web_ext("lint", "--source-dir=.")


def build(_arguments):
    read_manifest()

    return web_ext("build", f"--artifacts-dir={ARTIFACTS}", "--overwrite-dest")


def bump(arguments):
    text = MANIFEST.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(text)

    if not match:
        refuse(f'{MANIFEST} has no "version".')

    current = match.group(2)
    version = arguments.version or next_version(current)

    if not re.fullmatch(r"\d+(\.\d+)*", version):
        refuse(f"{version!r} is not a dotted number version.")

    if version_key(version) <= version_key(current):
        refuse(f"{version} is not higher than {current}.")

    MANIFEST.write_text(VERSION_PATTERN.sub(lambda found: f"{found.group(1)}{version}{found.group(3)}", text, count=1), encoding="utf-8")
    print(f"{current} -> {version}")

    return 0


def sign(arguments):
    version = read_manifest()["version"]

    if version in signed_versions():
        refuse(f"version {version} already has a signed .xpi in {ARTIFACTS.name}/ - run `bump` first.")

    missing = missing_credentials()

    if missing:
        refuse(f"set {' and '.join(missing)} in the environment (addons.mozilla.org -> Developer Hub -> API keys).")

    code = lint(arguments)

    if code:
        return code

    environment = {**os.environ, "WEB_EXT_API_KEY": os.environ[KEY_VARIABLE], "WEB_EXT_API_SECRET": os.environ[SECRET_VARIABLE]}
    code = web_ext("sign", f"--channel={'listed' if arguments.listed else 'unlisted'}", f"--artifacts-dir={ARTIFACTS}",
                   environment=environment)

    if code == 0:
        print(f"\nInstall: Firefox -> Add-ons -> gear -> Install Add-on From File -> {ARTIFACTS}/*-{version}.xpi")

    return code


def upload_target(version):
    """The WebDAV folders below the user's root and the file name for this version."""
    folder = project.get("browser_extension.nextcloud_folder", "Internal Programs")
    name = project.get("browser_extension.nextcloud_project", project.root.name)
    file_name = f"{name.lower()}-ext-{version}.xpi"

    return [*folder.split("/"), name, DIRECTORY.name], file_name


def wallet_secret(key):
    """The value stored under `key` in the default KDE Wallet; never printed."""
    try:
        result = subprocess.run(["kwallet-query", "-r", key, "kdewallet"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        refuse(f"could not read {key} from kdewallet: {error}")

    secret = result.stdout.strip()

    if not secret:
        refuse(f"{key} is empty in kdewallet.")

    return secret


def webdav_request(method, url, authorization, data=None):
    request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": authorization})

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def upload(_arguments):
    version = read_manifest()["version"]
    candidates = sorted(ARTIFACTS.glob(f"*-{version}.xpi"))

    if not candidates:
        refuse(f"version {version} has no signed .xpi in {ARTIFACTS.name}/ - run `sign` first.")

    missing = [name for name in (NEXTCLOUD_USER, NEXTCLOUD_WALLET_KEY) if not os.environ.get(name)]

    if missing:
        refuse(f"set {' and '.join(missing)} in the environment (the user, and the KDE Wallet key holding its app password).")

    user = os.environ[NEXTCLOUD_USER]
    authorization = "Basic " + base64.b64encode(f"{user}:{wallet_secret(os.environ[NEXTCLOUD_WALLET_KEY])}".encode()).decode()
    base = (project.get("browser_extension.nextcloud_url") or os.environ.get("DEVSCRIPTS_NEXTCLOUD_URL") or "").rstrip("/")

    if not base:
        refuse("set [browser_extension] nextcloud_url in project.toml, or DEVSCRIPTS_NEXTCLOUD_URL in the environment.")

    root = f"{base}/remote.php/dav/files/{urllib.parse.quote(user)}"
    folders, file_name = upload_target(version)

    path = ""

    for folder in folders:
        path += "/" + urllib.parse.quote(folder)
        code = webdav_request("MKCOL", root + path, authorization)

        if code not in (201, 405):
            refuse(f"Nextcloud answered {code} creating {path.lstrip('/')} (401 is a wrong user or app password).")

    code = webdav_request("PUT", f"{root}{path}/{urllib.parse.quote(file_name)}", authorization, candidates[-1].read_bytes())

    if code not in (201, 204):
        refuse(f"Nextcloud answered {code} uploading {file_name}.")

    print(f"{'/'.join(folders)}/{file_name}  ({'replaced' if code == 204 else 'new'})")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status").set_defaults(run=status)
    commands.add_parser("lint").set_defaults(run=lint)
    commands.add_parser("build").set_defaults(run=build)

    bump_parser = commands.add_parser("bump")
    bump_parser.add_argument("version", nargs="?", help="Instead of raising the last number")
    bump_parser.set_defaults(run=bump)

    sign_parser = commands.add_parser("sign")
    sign_parser.add_argument("--listed", action="store_true", help="The public directory instead of unlisted")
    sign_parser.set_defaults(run=sign)

    commands.add_parser("upload").set_defaults(run=upload)

    arguments = parser.parse_args()

    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main())
