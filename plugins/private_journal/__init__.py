"""Private journal plugin."""

from __future__ import annotations

from .capture import capture_log


def _handle_log(raw_args: str) -> str:
    return capture_log(raw_args)


def register(ctx) -> None:
    ctx.register_command(
        "log",
        _handle_log,
        description="Capture a private journal note",
        args_hint="<text>",
        verbatim_args=True,
        inline_while_busy=True,
    )
    ctx.register_auxiliary_task(
        key="private_journal_batch",
        display_name="Private journal batch",
        description="Extract structured private journal fields for pending /log captures.",
        defaults={"timeout": 120, "max_tokens": 4096},
    )
