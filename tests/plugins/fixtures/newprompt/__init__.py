"""Plain or parenthesized /new prompts travel with the native confirmation event."""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from pathlib import Path

from hermes_constants import get_hermes_home, hermes_home_key

_FIELD = "plugin_newprompt"
_TTL = 300


def register(ctx):
    owner = hermes_home_key(Path(ctx._manager.home_path))
    instance = secrets.token_hex(32)
    seen = OrderedDict()

    def ingress(event, gateway=None, **_):
        if gateway is None or event.get_command() != "new":
            return None
        args = (event.get_command_args() or "").strip()
        if not args:
            return None
        prompt = args[1:-1].strip() if args.startswith("(") and args.endswith(")") else args
        if hermes_home_key(get_hermes_home()) != owner:
            return None
        if not gateway._is_user_authorized_for_source(event.source):
            return None
        if not ctx._gateway_injection_allowed():
            return {"action": "skip", "reason": "newprompt gateway injection is not enabled"}
        key = gateway._session_key_for_source(event.source)
        # Native Discord slash events carry the authenticated interaction but omit message_id.
        if not event.message_id and event.source.platform.value == "discord":
            from discord import Interaction
            if isinstance(event.raw_message, Interaction):
                event.message_id = str(event.raw_message.id)
        if not event.message_id:
            return {"action": "skip", "reason": "newprompt requires a transport message id"}
        identity = (key, event.source.user_id, event.message_id)
        now = time.monotonic()
        while seen and (next(iter(seen.values())) < now - 600 or len(seen) >= 4096):
            seen.popitem(last=False)
        if identity in seen:
            return {"action": "skip", "reason": "newprompt duplicate transport delivery"}
        seen[identity] = now
        event.metadata = dict(event.metadata)
        event.metadata[_FIELD] = {
            "instance": instance, "prompt": prompt,
            "created": now, "session_key": key,
        }
        return {"action": "rewrite", "text": "/new"}

    def reset(session_key=None, new_session_id=None, command_metadata=None, **_):
        envelope = (command_metadata or {}).get(_FIELD)
        if not isinstance(envelope, dict) or envelope.get("instance") != instance:
            return
        if (hermes_home_key(get_hermes_home()) != owner
                or envelope.get("session_key") != session_key or not new_session_id
                or time.monotonic() - envelope["created"] > _TTL):
            return
        prompt = envelope.pop("prompt", "")
        if prompt:
            ctx.inject_message(prompt, session_key=session_key, expected_session_id=new_session_id)

    ctx.register_hook("pre_gateway_dispatch", ingress)
    ctx.register_hook("on_session_reset", reset)
    from .discord_commands import register_discord
    register_discord(ctx, instance)
