import { renderHook } from '@testing-library/react'
import { expect, it } from 'vitest'

import { overlayConcurrentMessageChanges } from '@/app/session/hooks/use-session-actions/utils'
import { appendAssistantTextPart, type ChatMessage, chatMessageText } from '@/lib/chat-messages'
import { projectReconnectReplies } from '@/lib/chat-messages/reconnect-reply'

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

it.each([false, true])('keeps the established pair across fresh pages (late completion: %s)', completed => {
  const prompt = row('16-user', 'user', 'across a drop', { rowId: 16 })
  const live = row('assistant-stream-1', 'assistant', 'A8', { pending: true })
  const freshPage = () => [prompt, row('17-assistant', 'assistant', 'A8 survives the drop', { rowId: 17 })]
  const first = overlayConcurrentMessageChanges(freshPage(), [prompt], [prompt, live])
  const updated = { ...live, parts: appendAssistantTextPart(live.parts, ' survives') }
  const current = first.map(message => (message.id === live.id ? updated : message))
  let messages = overlayConcurrentMessageChanges(freshPage(), first, current)

  const { result, rerender } = renderHook(({ messages }) => useRuntimeMessageRepository(messages), {
    initialProps: { messages }
  })

  const answers = () => result.current.messages.filter(({ message }) => message.role === 'assistant')

  expect(answers()).toHaveLength(1)
  expect(answers()[0].message.content).toContainEqual({ type: 'text', text: 'A8 survives the drop' })
  expect(messages.find(message => message.id === live.id)).toBe(updated)
  expect(chatMessageText(updated)).toBe('A8 survives')

  const baseline = messages
  const settled = { ...updated, pending: false, rowId: 17, durableComplete: true }

  if (completed) {
    messages = messages.map(message => (message.id === live.id ? settled : message))
  }

  messages = overlayConcurrentMessageChanges(freshPage(), baseline, messages)
  rerender({ messages })
  expect(answers()).toHaveLength(1)
  expect(answers()[0].message.content).toContainEqual({ type: 'text', text: 'A8 survives the drop' })
  expect(messages.find(message => message.id === live.id)).toBe(completed ? settled : updated)
  expect(chatMessageText(messages.find(message => message.id === live.id)!)).toBe('A8 survives')
})

it('keeps a preserved raw cursor before the next prompt across repeated hydration', () => {
  const prompt = row('16-user', 'user', 'first', { rowId: 16 })
  const live = row('assistant-stream-1', 'assistant', 'answer', { pending: true })
  const stored = row('17-assistant', 'assistant', 'answer tail', { rowId: 17, liveReplyId: live.id })
  const queued = row('18-user', 'user', 'next', { rowId: 18 })
  const baseline = [prompt, stored, live, queued]
  const freshPage = () => [prompt, { ...stored, liveReplyId: undefined }, queued]
  const second = overlayConcurrentMessageChanges(freshPage(), baseline, baseline)
  const third = overlayConcurrentMessageChanges(freshPage(), second, second)

  expect(second.findIndex(message => message.id === live.id)).toBeLessThan(
    second.findIndex(message => message.id === queued.id)
  )
  expect(projectReconnectReplies(third).map(chatMessageText)).toEqual(['first', 'answer tail', 'next'])
  expect(third.find(message => message.id === live.id)).toBe(live)
})

it.each([
  ['same', 'same answer'],
  ['same answer', 'same answer'],
  ['same answer and more', 'same answer and more']
])('projects one anchored answer when the initial live text is %s', (text, expected) => {
  const prompt = row('user-16', 'user', 'repeat', { rowId: 16 })
  const live = row('assistant-stream-1', 'assistant', text, { pending: true })
  const stored = row('17-assistant', 'assistant', 'same answer', { rowId: 17 })
  const messages = overlayConcurrentMessageChanges([prompt, stored], [prompt], [prompt, live])
  const projected = projectReconnectReplies(messages)

  expect(projected.map(chatMessageText)).toEqual(['repeat', expected])
  expect(messages.find(message => message.id === live.id)).toBe(live)
  expect(chatMessageText(live)).toBe(text)
})

it('keeps repeated prose in separate prompt occurrences and unrepresented steering', () => {
  const oldPrompt = row('16-user', 'user', 'repeat', { rowId: 16 })
  const newPrompt = row('18-user', 'user', 'repeat', { rowId: 18 })
  const stored = row('17-assistant', 'assistant', 'same answer', { rowId: 17 })
  const live = row('assistant-stream-2', 'assistant', 'same answer', { pending: true })
  const queued = row('user-queued-3', 'user', 'next')
  const current = [oldPrompt, stored, newPrompt, live, queued]
  const messages = overlayConcurrentMessageChanges([oldPrompt, stored], [oldPrompt], current)

  expect(projectReconnectReplies(messages).map(chatMessageText)).toEqual([
    'repeat',
    'same answer',
    'repeat',
    'same answer',
    'next'
  ])

  const steering = row('steer', 'user', 'change course', { hidden: true })
  const steered = overlayConcurrentMessageChanges([newPrompt, stored], [], [newPrompt, steering, live])

  expect(projectReconnectReplies(steered).filter(message => message.role === 'assistant')).toHaveLength(2)
})

it.each([false, true])('preserves reasoning, tools, error and queue (structure present at refresh: %s)', atRefresh => {
  const prompt = row('16-user', 'user', 'prompt', { rowId: 16 })
  const live = row('assistant-stream-1', 'assistant', 'answer', { pending: true })
  const stored = row('17-assistant', 'assistant', 'answer tail', { rowId: 17 })
  const tool = { type: 'tool-call', toolCallId: 'new-call', toolName: 'terminal', args: {}, argsText: '{}' } as const
  const reasoning = { type: 'reasoning', text: 'new thought' } as const
  const structured = { ...live, parts: [...live.parts, reasoning, tool] }
  const messages = overlayConcurrentMessageChanges([prompt, stored], [prompt], [prompt, atRefresh ? structured : live])
  const updated = { ...structured, error: 'disconnected', pending: false }
  const queued = row('user-queued-2', 'user', 'next')

  const projected = projectReconnectReplies([
    ...messages.map(message => (message.id === live.id ? updated : message)),
    queued
  ])

  expect(projected.map(chatMessageText)).toEqual(['prompt', 'answer tail', 'next'])
  expect(projected[1].parts).toContain(reasoning)
  expect(projected[1].parts).toContain(tool)
  expect(projected[1].error).toBe('disconnected')
  expect(chatMessageText(updated)).toBe('answer')
})
