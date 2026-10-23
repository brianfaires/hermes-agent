import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { checkPrivateCommand } from '../app/privateCommands/dispatch.js'

const previousHome = process.env.HERMES_HOME

afterEach(() => {
  if (previousHome === undefined) {
    delete process.env.HERMES_HOME
  } else {
    process.env.HERMES_HOME = previousHome
  }

  vi.resetModules()
})

describe('private submission boundary', () => {
  it('requires an explicit ordinary result before allowing transcript/history dispatch', async () => {
    const acknowledge = vi.fn()
    const continuation = vi.fn()
    const request = vi.fn(async () => ({ handled: true, output: 'Logged receipt' }))
    const handled = await checkPrivateCommand('/log FICTIONAL_PRIVATE', request, acknowledge)

    if (!handled) {
      continuation()
    }

    expect(request).toHaveBeenCalledWith('/log FICTIONAL_PRIVATE')
    expect(continuation).not.toHaveBeenCalled()
    expect(acknowledge).toHaveBeenCalledWith('Logged receipt')
    expect(await checkPrivateCommand('/help', async () => ({ handled: false }), acknowledge)).toBe(false)
  })

  it('fails closed and does not echo request failures or missing responses', async () => {
    const acknowledge = vi.fn()
    expect(await checkPrivateCommand('/log FICTIONAL_PRIVATE', async () => {
      throw Error('FICTIONAL_PRIVATE')
    }, acknowledge)).toBe(true)
    expect(await checkPrivateCommand('/log FICTIONAL_PRIVATE', async () => undefined, acknowledge)).toBe(true)
    expect(JSON.stringify(acknowledge.mock.calls)).not.toContain('FICTIONAL_PRIVATE')
  })

  it('does not save discarded slash drafts, while verified ordinary commands and bang input persist', async () => {
    const home = mkdtempSync(join(tmpdir(), 'hermes-private-history-'))
    process.env.HERMES_HOME = home
    const history = await import('../lib/history.js')
    history.append('/log FICTIONAL_PRIVATE')
    history.append('/status', true)
    history.append('!printf ordinary')
    expect(history.load()).toEqual(['/status', '!printf ordinary'])
    expect(readFileSync(join(home, '.hermes_history'), 'utf8')).not.toContain('FICTIONAL_PRIVATE')
  })
})
