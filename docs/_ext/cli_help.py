# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The ``udevmon-help`` directive: the CLI reference, from the argparse parser.

Nothing is run: the parser is built (``udevmon.cli.build_parser``, which has no side
effects) and its help text is formatted.
"""

import argparse

from docutils import nodes
from docutils.parsers.rst import Directive


def parser_help(parser, command="udevmon"):
    """Yield ``(command, help text)`` for the parser and each of its subcommands.

    Walks argparse's private ``_actions`` / ``_SubParsersAction``; the generated-help
    test in ``tests/test_documentation.py`` catches a change there.
    """
    yield command, parser.format_help()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from parser_help(child, f"{command} {name}")


class CommandHelp(Directive):
    """A section with the help of each command and subcommand."""

    def run(self):
        from udevmon.cli import build_parser

        result = []
        for command, help_text in parser_help(build_parser()):
            section = nodes.section(ids=[command.replace(" ", "-")])
            section += nodes.title(text=command)
            section += nodes.literal_block(text=help_text, language="text")
            result.append(section)
        return result


def setup(app):
    app.add_directive("udevmon-help", CommandHelp)
    return {"version": "1", "parallel_read_safe": True}
