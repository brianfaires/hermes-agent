import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('input history persistence', () => {
  let home: string

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'hermes-history-'))
    vi.resetModules()
    vi.stubEnv('HERMES_HOME', home)
  })

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('fails closed for slash-like drafts until the private catalog is known', async () => {
    const { append } = await import('../lib/history.js')
    const { resetPrivateCommands, setPrivateCommands } = await import('../app/privateCommands/state.js')

    const historyFile = join(home, '.hermes_history')
    const readHistory = () => readFileSync(historyFile, 'utf8')

    append('ordinary text')
    const afterPlainText = readHistory()

    append('/help show me')
    expect(readHistory()).toBe(afterPlainText)

    setPrivateCommands(['/log'])
    append('/log SECRET_SENTINEL')
    expect(readHistory()).toBe(afterPlainText)

    resetPrivateCommands()
    append('/help still blocked while catalog is cold')
    expect(readHistory()).toBe(afterPlainText)

    setPrivateCommands([])
    append('/help now allowed after catalog recovery')

    const finalHistory = readHistory()
    expect(finalHistory).toContain('+ordinary text')
    expect(finalHistory).toContain('+/help now allowed after catalog recovery')
    expect(finalHistory).not.toContain('+/log SECRET_SENTINEL')
  })
})
