import assert from 'node:assert/strict'
import { test, vi } from 'vitest'

// Load the actual hook with headless transport/render adapters; no React/Ink installation is required.
const fixture = {
  useCallback: fn => fn, useEffect: () => {}, useRef: value => ({ current: value }),
  looksLikeSlashCommand: text => text.startsWith('/'),
  parseSlashCommand: text => ({ name: text.trim().split(/\s/)[0].slice(1) }),
  TYPING_IDLE_MS: 500, expandTokens: tokens => text => tokens.reduce((value, token) => value.replace(token.label, token.text), text), imageAttachments: () => [],
  completionToApplyOnSubmit: () => undefined, queueItem: text => ({ text }), t: text => text,
  asRpcResult: value => value, hasInterpolation: () => false, INTERPOLATION_RE: /\$\((.*?)\)/g,
  reportSlashCommand: () => {}, findSlashCommand: name => name === 'help', submitPrompt: () => {},
  captureDestination: () => ({ sid: 's', profile: 'default' }), isCurrentDestination: () => true,
  turnController: {}, getUiState: () => ({ sid: 's', busy: true }), patchUiState: () => {},
  describeShellExecError: () => 'Shell failed'
}
for (const dependency of [
  'react', '@hermes/shared/slash', '../ui-tui/src/config/timing.js',
  '../ui-tui/src/domain/attachments.js', '../ui-tui/src/domain/slash.js',
  '../ui-tui/src/hooks/useQueue.js', '../ui-tui/src/i18n/runtime.js',
  '../ui-tui/src/lib/rpc.js', '../ui-tui/src/protocol/interpolation.js',
  '../ui-tui/src/app/createSlashHandler.js', '../ui-tui/src/app/slash/registry.js',
  '../ui-tui/src/app/submissionCore.js', '../ui-tui/src/app/submissionDestination.js',
  '../ui-tui/src/app/turnController.js', '../ui-tui/src/app/uiStore.js',
  '../ui-tui/src/app/userMessages.js'
]) vi.doMock(dependency, () => fixture)
const { useSubmission } = await import('../ui-tui/src/app/useSubmission.ts')

test('pasted multiline /log uses ordinary slash dispatch with complete text', () => {
  const ordinary = [], history = []
  const refs = { tokensRef: { current: [] } }
  const hook = useSubmission({
    appendMessage: () => {}, composerActions: { clearIn: () => {}, pushHistory: text => history.push(text) },
    composerRefs: refs, composerState: { input: '', inputBuf: [] },
    gw: { request: () => assert.fail('no extra RPC') }, setLastUserMsg: () => {},
    slashRef: { current: text => ordinary.push(text) }, submitRef: { current: () => {} }, sys: () => {}
  })
  hook.dispatchSubmission('/log  exact\n  line\n')
  refs.tokensRef.current = [{ kind: 'paste', label: '[[paste]]', text: '   /log  pasted\n\n  line\n' }]
  hook.dispatchSubmission('[[paste]]')
  hook.dispatchSubmission('/help')
  assert.deepEqual(ordinary, ['/log  exact\n  line\n', '/log  pasted\n\n  line\n', '/help'])
  assert.equal(history.length, 3)
})
