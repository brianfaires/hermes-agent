import { chatMessageText } from './parts'
import type { ChatMessage } from './types'

/** Project only twins explicitly paired by hydration, never equal prose globally. */
export function projectReconnectReplies(messages: ChatMessage[]): ChatMessage[] {
  const byId = new Map(messages.map(message => [message.id, message]))
  const replacements = new Map<string, ChatMessage>()
  const covered = new Set<string>()

  for (const stored of messages) {
    const live = stored.liveReplyId ? byId.get(stored.liveReplyId) : undefined

    if (
      stored.role !== 'assistant' ||
      !live ||
      live.hidden ||
      live.role !== 'assistant' ||
      stored.error ||
      stored.parts.some(part => part.type !== 'text')
    ) {
      continue
    }

    const committedText = chatMessageText(stored)
    const liveText = chatMessageText(live)

    if (!liveText || !(committedText.startsWith(liveText) || liveText.startsWith(committedText))) {
      continue
    }

    // Extend only the display's last text part. All live errors, tools and
    // reasoning stay on the live row; its raw parts remain the delta cursor.
    const tail = committedText.slice(liveText.length)
    const lastText = live.parts.findLastIndex(part => part.type === 'text')

    const projected = tail
      ? {
          ...live,
          parts: live.parts.map((part, index) =>
            index === lastText && part.type === 'text' ? { ...part, text: part.text + tail } : part
          )
        }
      : live

    replacements.set(stored.id, projected)
    covered.add(live.id)
  }

  return covered.size
    ? messages.filter(message => !covered.has(message.id)).map(message => replacements.get(message.id) ?? message)
    : messages
}
