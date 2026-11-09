"""Conversational speech at native compression serialization boundaries."""
import json
import re


def spoken_text_key(text):
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'(?<!\w)(\*\*|__|`)(\S(?:.*?\S)?)\1(?!\w)', r'\2', text)
    text = ' '.join(text.split())
    return text


def summary_text(message):
    """Extract visible content plus distinct complete TTS text, before truncation."""
    from agent import context_compressor as cc

    content = message.get('content')
    visible = ('\n'.join(cc._summary_part_text(part) for part in content
                         if isinstance(part, (dict, str)))
               if isinstance(content, list) else cc._content_text_for_contains(content))
    if message.get('role') != 'assistant':
        return visible
    seen = {spoken_text_key(visible)}
    parts = [visible] if visible else []
    for call in message.get('tool_calls') or []:
        name, raw = cc._extract_tool_call_name_and_args(call)
        if name != 'text_to_speech':
            continue
        try:
            arguments = json.loads(raw)
        except (TypeError, ValueError):
            continue
        speech = arguments.get('text') if isinstance(arguments, dict) else None
        if isinstance(speech, str) and (key := spoken_text_key(speech)) and key not in seen:
            parts.append('[Spoken assistant output]: ' + speech.strip())
            seen.add(key)
    return '\n'.join(parts)


def serialize_records(compressor, turns):
    """Retain speech, omit paired delivery results, use native redaction/bounds."""
    from agent import context_compressor as cc
    from agent.agent_runtime_helpers import strip_think_blocks

    tts_ids = {call_id for call_id, (name, _) in cc._tool_calls_by_id(turns).items()
               if call_id and name == 'text_to_speech'}
    records = []
    for message in turns:
        role = message.get('role', 'unknown')
        if role == 'tool' and message.get('tool_call_id') in tts_ids:
            continue
        content = cc._redact_compaction_text(summary_text(message))
        content = cc._MEDIA_DIRECTIVE_RE.sub('[media attachment]', content)
        if role == 'assistant' and content:
            content = strip_think_blocks(None, content)
        if len(content) > compressor._CONTENT_MAX:
            content = cc.elide_middle(content, compressor._CONTENT_HEAD, compressor._CONTENT_TAIL)
        if role == 'tool':
            records.append(f"[TOOL RESULT {message.get('tool_call_id', '')}]: {content}")
            continue
        if role == 'assistant' and message.get('tool_calls'):
            calls = []
            for call in message['tool_calls']:
                name, _ = cc._extract_tool_call_name_and_args(call)
                calls.append('  text_to_speech({})' if name == 'text_to_speech'
                             else compressor._render_tool_call_for_summary(call))
            content += '\n[Tool calls:\n' + '\n'.join(calls) + '\n]'
        records.append(f'[{role.upper()}]: {content}')
    return records


def fallback_assistant_actions(text, tool_names):
    """Keep spoken content even when its message also called ordinary tools."""
    actions = []
    if any(name != 'text_to_speech' for name in tool_names):
        actions.append('Called tool(s): ' + ', '.join(tool_names[:6]))
    if text and (not tool_names or 'text_to_speech' in tool_names):
        actions.append(text)
    return actions
