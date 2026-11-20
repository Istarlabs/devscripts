# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-check for tidy.py: `python3 tidy.test.py`."""
import importlib.util
import os
import pathlib
import tempfile
import time

spec = importlib.util.spec_from_file_location("tidy", pathlib.Path(__file__).with_name("tidy.py"))
tidy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tidy)

with tempfile.TemporaryDirectory() as scratch:
    scratch = pathlib.Path(scratch)
    root = scratch / "dl"
    root.mkdir()
    old = time.time() - 3600
    for name in ["a.pdf", "Screenshot 1.png", "photo.jpg", "setup.rpm", "sdk.tar.gz", "half.pdf.part", "new.pdf", "mine.dat", "x.bin2"]:
        (root / name).write_text("x")
        os.utime(root / name, (old, old))
    os.utime(root / "new.pdf", None)  # just downloaded: stays
    (root / "folder").mkdir()

    config = scratch / "tidy.toml"
    config.write_text("[[rule]]\nfolder = 'Private'\nmatch = '^mine'\n")
    tidy.UNDO_LOG = scratch / "undo.json"

    moves, left = tidy.plan(root, tidy.load_rules(config))
    where = {s.name: t.parent.relative_to(root).as_posix() for s, t in moves}
    assert where == {"a.pdf": "Documents", "Screenshot 1.png": "Pictures/Screenshots", "photo.jpg": "Pictures",
                     "setup.rpm": "Installers", "sdk.tar.gz": "Archives", "mine.dat": "Private"}, where
    # a folder, an unfinished download, a fresh file and an unknown type stay
    assert sorted(left) == ["folder", "half.pdf.part", "new.pdf", "x.bin2"], left

    (root / "Documents").mkdir()
    (root / "Documents" / "a.pdf").write_text("taken")
    assert len(tidy.apply(moves)) == 5  # a.pdf is skipped: the name is taken
    assert (root / "a.pdf").exists() and (root / "Pictures" / "photo.jpg").exists()

    assert tidy.undo() == 5
    assert (root / "photo.jpg").exists() and not (root / "Pictures" / "photo.jpg").exists()
print("ok")
