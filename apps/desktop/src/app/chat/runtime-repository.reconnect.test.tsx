import { renderHook } from '@testing-library/react'
import { expect, it } from 'vitest'

import { overlayConcurrentMessageChanges } from '@/app/session/hooks/use-session-actions/utils'
import { appendAssistantTextPart, type ChatMessage, chatMessageText } from '@/lib/chat-messages'

import { useRuntimeMessageRepository } from './runtime-repository'

const row = (id: string, role: ChatMessage['role'], text: string, extra = {}): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text }],
  ...extra
})

it('renders one complete reconnect answer through late deltas without advancing the raw stream cursor', () => {
  const prompt = row('16-user', 'user', 'across a drop', { rowId: 16 })
  const baseline = [prompt, row('assistant-stream-1', 'assistant', 'A8', { pending: true })]
  const current = [prompt, row('assistant-stream-1', 'assistant', 'A8 survives the', { pending: true })]
  const page = [prompt, row('17-assistant', 'assistant', 'A8 survives the drop', { rowId: 17 })]
  let messages = overlayConcurrentMessageChanges(page, baseline, current)

  const { result, rerender } = renderHook(({ messages }) => useRuntimeMessageRepository(messages), {
    initialProps: { messages }
  })

  const answers = () =>
    result.current.messages
      .filter(({ message }) => message.role === 'assistant')
      .map(({ message }) =>
        message.content
          .filter(part => part.type === 'text')
          .map(part => part.text)
          .join('')
      )

  // A lost completion leaves the full persisted answer available indefinitely.
  expect(answers()).toEqual(['A8 survives the drop'])
  expect(chatMessageText(messages.find(message => message.id === 'assistant-stream-1')!)).toBe('A8 survives the')
  rerender({ messages })
  expect(answers()).toEqual(['A8 survives the drop'])

  for (const delta of [' drop', ' and more']) {
    messages = messages.map(message =>
      message.id === 'assistant-stream-1'
        ? { ...message, parts: appendAssistantTextPart(message.parts, delta) }
        : message
    )
    rerender({ messages })
    expect(answers()).toEqual([delta === ' drop' ? 'A8 survives the drop' : 'A8 survives the drop and more'])
  }

  messages = messages.map(message =>
    message.id === 'assistant-stream-1' ? { ...message, pending: false, rowId: 17, durableComplete: true } : message
  )
  rerender({ messages })
  expect(answers()).toEqual(['A8 survives the drop and more'])
})
