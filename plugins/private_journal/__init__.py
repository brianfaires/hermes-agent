"""Disabled-by-default personal capture plugin; no model tools or hooks."""
from .capture import capture_log


def _handle_log(raw_args, *, home):
    return capture_log(raw_args, home=home)


def register(ctx):
    ctx.register_command('log', _handle_log,
                         description='Capture a private journal note',
                         args_hint='<text>', private=True)
