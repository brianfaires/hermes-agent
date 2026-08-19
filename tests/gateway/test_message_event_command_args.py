from gateway.platforms.base import MessageEvent


def test_get_command_args_raw_preserves_delivered_text_after_command_delimiter():
    event = MessageEvent(text="/log   alpha — beta  ")

    assert event.get_command_args_raw() == "  alpha — beta  "
    assert event.get_command_args() == "alpha -- beta  "


def test_get_command_args_raw_handles_no_arguments():
    assert MessageEvent(text="/log").get_command_args_raw() == ""
