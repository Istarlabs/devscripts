# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Argument handling every project script shares: --help prints the script's docstring, an argument the script does not
know is refused before it does anything.

A script that ignored what it did not understand ran its default - `apply_routines.py --help` applied every routine
to a database. From a script outside this checkout:

    sys.path.insert(0, os.environ.get("DEVSCRIPTS", os.path.expanduser("~/dev/tools/devscripts")))
    import script_args

    script_args.no_arguments(__doc__)               a script that takes none
    args = script_args.parser(__doc__)...           one that takes some: add_argument, then parse_args()
"""
import argparse


def parser(doc):
    """An ArgumentParser whose --help is the script's docstring, laid out as written."""
    return argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)


def no_arguments(doc):
    """Handle --help and refuse anything else."""
    parser(doc).parse_args()
