import { describe, it, expect, vi } from 'vitest'
import { checkPrivateCommand } from '../app/privateCommands/dispatch.js'
import { isPrivateCommand, setPrivateCommands } from '../app/privateCommands/state.js'

describe('private dispatch before transcript and history', () => {
  it('preserves delivered bytes and only acknowledges the result', async () => {
    const text = '/log   unicode —\n  '
    const request = vi.fn(async () => ({ handled: true, output: 'Logged id at time' }))
    const ack = vi.fn()
    expect(await checkPrivateCommand(text, request, ack)).toBe(true)
    expect(request).toHaveBeenCalledWith(text)
    expect(ack).toHaveBeenCalledWith('Logged id at time')
  })
  it('only allows an explicit non-private result to reach ordinary dispatch', async () => {
    const ack = vi.fn()
    expect(await checkPrivateCommand('/status', async () => ({ handled: false }), ack)).toBe(false)
    expect(ack).not.toHaveBeenCalled()
    expect(await checkPrivateCommand('/log secret', async () => { throw Error('secret') }, ack)).toBe(true)
    expect(JSON.stringify(ack.mock.calls)).not.toContain('secret')
    expect(await checkPrivateCommand('/log secret', async () => undefined, ack)).toBe(true)
  })
  it('guards draft history using plugin metadata without changing bang commands', () => {
    setPrivateCommands(['/log'])
    expect(isPrivateCommand('/log  secret')).toBe(true)
    expect(isPrivateCommand('!log')).toBe(false)
    expect(isPrivateCommand('/logger ordinary')).toBe(false)
    setPrivateCommands([])
    expect(isPrivateCommand('/log')).toBe(false)
  })
})
