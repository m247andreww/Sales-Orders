"""Every command handler is reachable from the command line (guards against silently unregistered commands)."""

from __future__ import annotations

import argparse

from sales_orders import cli


def _registered_handlers() -> set[str]:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {p.get_default("func").__name__ for p in sub.choices.values()}


def test_every_cmd_function_is_registered() -> None:
    defined = {name for name in dir(cli) if name.startswith("cmd_")}
    assert defined == _registered_handlers()


def test_help_lists_all_commands() -> None:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) >= {
        "load-order",
        "load-register",
        "assign-sn",
        "build-account-history",
        "allocate-account",
        "employee-leaves",
        "link-arr",
        "arr-outstanding",
    }
