from cron.scheduler import _delivery_target_lost_origin_thread


def test_cross_platform_home_delivery_does_not_count_as_lost_origin_thread():
    origin = {
        "platform": "discord",
        "chat_id": "1520346818848682004",
        "thread_id": "1520428590385074296",
    }
    target = {
        "platform": "telegram",
        "chat_id": "8244556262",
        "thread_id": None,
    }

    assert _delivery_target_lost_origin_thread(origin, target) is False


def test_same_platform_different_home_chat_does_not_count_as_lost_origin_thread():
    origin = {
        "platform": "discord",
        "chat_id": "origin-channel",
        "thread_id": "topic-42",
    }
    target = {
        "platform": "discord",
        "chat_id": "home-channel",
        "thread_id": None,
    }

    assert _delivery_target_lost_origin_thread(origin, target) is False


def test_same_platform_and_chat_without_thread_counts_as_lost_origin_thread():
    origin = {
        "platform": "discord",
        "chat_id": "channel-1",
        "thread_id": "topic-42",
    }
    target = {
        "platform": "discord",
        "chat_id": "channel-1",
        "thread_id": None,
    }

    assert _delivery_target_lost_origin_thread(origin, target) is True


def test_target_with_thread_does_not_count_as_lost_origin_thread():
    origin = {
        "platform": "discord",
        "chat_id": "channel-1",
        "thread_id": "topic-42",
    }
    target = {
        "platform": "discord",
        "chat_id": "channel-1",
        "thread_id": "topic-42",
    }

    assert _delivery_target_lost_origin_thread(origin, target) is False
