"""One shared ``tessera dev`` command group.

Several developer helpers (focused test selection, smoke credentials) live under ``tessera dev``.
argparse cannot register the same subcommand twice, so each helper asks this module for the shared
action group; the first caller creates the ``dev`` parser and later callers add to it. The chosen
action is stored in ``dev_action`` and the ``dev`` branch of the dispatcher routes on it.
"""

from __future__ import annotations

import argparse


def dev_actions(subcommands: argparse._SubParsersAction) -> argparse._SubParsersAction:
    parser = subcommands.choices.get("dev")
    if parser is None:
        parser = subcommands.add_parser("dev", help="developer commands that need a checkout")
        parser._tessera_dev_actions = parser.add_subparsers(  # type: ignore[attr-defined]
            dest="dev_action", required=True
        )
    return parser._tessera_dev_actions  # type: ignore[attr-defined]
