from __future__ import annotations

from argparse import Namespace
from unittest import mock

from hermes_cli.main import cmd_cron


def test_cmd_cron_propagates_subcommand_exit_code() -> None:
    args = Namespace(cron_command="finalize-detached")

    with mock.patch("hermes_cli.cron.cron_command", return_value=2) as command:
        result = cmd_cron(args)

    command.assert_called_once_with(args)
    assert result == 2
