import { atom } from 'nanostores'

export const $privateCommands = atom<ReadonlySet<string>>(new Set())
export const $privateCommandsLoaded = atom(false)

export function setPrivateCommands(names: string[]) {
  $privateCommands.set(new Set(names.map(name => name.toLowerCase())))
  $privateCommandsLoaded.set(true)
}

export function resetPrivateCommands() {
  $privateCommands.set(new Set())
  $privateCommandsLoaded.set(false)
}

export function isPrivateCommand(text: string) {
  const name = text.trimStart().split(/\s/, 1)[0]?.toLowerCase()
  return !!name && $privateCommands.get().has(name)
}

export function hasPrivateCommandsCatalog() {
  return $privateCommandsLoaded.get()
}
