import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import { test } from 'node:test'

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
globalThis.textHookFixture = fixture
const adapters = 'data:text/javascript,' + encodeURIComponent(Object.keys(fixture)
  .map(name => `export const ${name} = globalThis.textHookFixture.${name}`).join('\n'))
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL?.endsWith('/app/useSubmission.ts')) {
      return { url: adapters, shortCircuit: true }
    }
    return nextResolve(specifier, context)
  }
})
const { useSubmission } = await import('../ui-tui/src/app/useSubmission.ts')
hooks.deregister()

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
