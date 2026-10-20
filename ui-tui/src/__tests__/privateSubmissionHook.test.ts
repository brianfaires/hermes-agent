import type * as ReactModule from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('react', async importOriginal => ({
  ...(await importOriginal<typeof ReactModule>()),
  useCallback: (callback: unknown) => callback,
  useEffect: () => undefined,
  useRef: (value: unknown) => ({ current: value })
}))

import { resetUiState } from '../app/uiStore.js'
import { useSubmission, type UseSubmissionOptions } from '../app/useSubmission.js'

function SubmissionHarness(request = vi.fn().mockRejectedValue(Error('gateway not running'))) {
  const slash = vi.fn()
  const clearIn = vi.fn()
  const sys = vi.fn()
  const pushHistory = vi.fn()

  const options = {
    appendMessage: vi.fn(),
    composerActions: { clearIn, pushHistory },
    composerRefs: { tokensRef: { current: [] } },
    composerState: { input: '', inputBuf: [], completions: [] },
    gw: { request },
    setLastUserMsg: vi.fn(),
    slashRef: { current: slash },
    submitRef: { current: vi.fn() },
    sys
  } as unknown as UseSubmissionOptions

  return { ...useSubmission(options), request, slash, clearIn, pushHistory, sys }
}

beforeEach(resetUiState)

describe('private command submission', () => {
  it.each(['/quit', '/help', '/clear'])('dispatches %s locally while the backend is down', command => {
    const state = SubmissionHarness()
    state.dispatchSubmission(command)
    expect(state.slash).toHaveBeenCalledWith(command)
    expect(state.request).not.toHaveBeenCalled()
    expect(state.pushHistory).toHaveBeenCalledWith(command, true)
  })

  it('consumes a pending submission once and leaves later drafts intact', async () => {
    let finish!: (value: unknown) => void

    const response = new Promise(resolve => {
      finish = resolve
    })

    const state = SubmissionHarness(vi.fn().mockReturnValue(response))
    state.dispatchSubmission('/log fixture')
    state.dispatchSubmission('/log fixture')
    expect(state.request).toHaveBeenCalledTimes(1)
    expect(state.clearIn).toHaveBeenCalledTimes(1)
    finish({ handled: true, output: 'Logged receipt' })
    await vi.waitFor(() => expect(state.sys).toHaveBeenCalledWith('Logged receipt'))
    expect(state.clearIn).toHaveBeenCalledTimes(1)
    expect(state.pushHistory).not.toHaveBeenCalled()
    expect(state.slash).not.toHaveBeenCalled()
  })
})
