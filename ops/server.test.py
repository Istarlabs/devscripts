#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-check for server.py's entry grouping: `python3 server.test.py`."""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("server", pathlib.Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

P = "Oct 14 10:46:12 web homeassistant[1]: "
journal = [
    P + "2026-10-14 10:46:12.001 ERROR (MainThread) [habluetooth.scanner] Failed to force stop scanner",
    P + "Traceback (most recent call last):",
    P + '  File "x.py", line 975, in stop',
    P + "AttributeError: 'NoneType' object has no attribute 'send'",
    P + "2026-10-14 10:49:46.001 ERROR (MainThread) [kasa] Query failed",
]

grouped = server.entries(journal)
assert [len(entry) for entry in grouped] == [4, 1], grouped

# a grep for the exception name must return the whole entry, message and trace together
assert server.select(grouped, "AttributeError", None) == [grouped[0]]
assert server.select(grouped, None, "scanner") == [grouped[1]]
server.SERVER["timezone"] = "Europe/Berlin"
unit = server.render_unit("something", "ghcr.io/vendor/something:latest", None, ["1234:8080"], ["A=b"],
                         ["config:/config"], ["backend"], True, "services")
for line in ("Description=something", "AutoUpdate=registry", "PublishPort=1234:8080", "Environment=TZ=Europe/Berlin",
             "Environment=A=b", "Volume=%h/services/something/config:/config:Z", "Network=backend", "WantedBy=default.target"):
    assert line in unit.splitlines(), line
assert "AutoUpdate" not in server.render_unit("x", "i", None, [], [], [], [], False, "services")
server.SERVER["timezone"] = ""
assert "TZ=" not in server.render_unit("x", "i", None, [], [], [], [], True, "services")
assert "%h/data/container/x/d:/d:Z" in server.render_unit("x", "i", None, [], [], ["d:/d"], [], True, "data/container")
# podman rejects a second colon: options the caller gives are joined with Z by a comma
assert server.volume_line("services", "x", "repo:/srv:ro") == "Volume=%h/services/x/repo:/srv:ro,Z"
print("ok")
