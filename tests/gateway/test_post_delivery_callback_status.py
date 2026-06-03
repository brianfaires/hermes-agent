"""Tests for ``BasePlatformAdapter.register_post_delivery_callback`` chaining."""

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter


class DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def test_post_delivery_callback_passes_delivery_status_to_callbacks_that_accept_it():
    adapter = DummyAdapter()
    seen = []

    adapter.register_post_delivery_callback(
        "s",
        lambda *, delivery_succeeded=False: seen.append(delivery_succeeded),
    )

    callback = adapter.pop_post_delivery_callback("s")
    callback(delivery_succeeded=True)

    assert seen == [True]


def test_post_delivery_callback_chaining_preserves_no_arg_callbacks():
    adapter = DummyAdapter()
    seen = []

    adapter.register_post_delivery_callback("s", lambda: seen.append("old"))
    adapter.register_post_delivery_callback(
        "s",
        lambda *, delivery_succeeded=False: seen.append(delivery_succeeded),
    )

    callback = adapter.pop_post_delivery_callback("s")
    callback(delivery_succeeded=False)

    assert seen == ["old", False]
