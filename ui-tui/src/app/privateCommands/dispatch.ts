import { looksLikeSlashCommand, parseSlashCommand } from '@hermes/shared/slash'

import { findSlashCommand } from '../slash/registry.js'

export interface PrivateCommandResult {
  handled?: boolean
  output?: string
}

export async function checkPrivateCommand(
  command: string,
  request: (command: string) => Promise<PrivateCommandResult | null | undefined>,
  acknowledge: (output: string) => void
): Promise<boolean> {
  try {
    const result = await request(command)

    if (result?.handled === false) {
      return false
    }

    acknowledge(
      result?.handled ? result.output || '(no output)' : 'Private command check failed; input was not submitted.'
    )
  } catch {
    acknowledge('Private command check failed; input was not submitted.')
  }

  return true
}

export const isLocalSlashCommand = (command: string) =>
  looksLikeSlashCommand(command) && Boolean(findSlashCommand(parseSlashCommand(command).name))

export interface PrivateSubmissionCheck {
  display: string
  command: string
  local: boolean
  pending: { current: boolean }
  clear: () => void
  request: (command: string) => Promise<PrivateCommandResult | null | undefined>
  acknowledge: (output: string) => void
  resume: () => void
}

export function beginPrivateSubmission(check: PrivateSubmissionCheck): boolean {
  if (!looksLikeSlashCommand(check.display) || check.local) {
    return false
  }

  if (check.pending.current) {
    return true
  }

  check.pending.current = true
  check.clear()
  void checkPrivateCommand(check.command, check.request, check.acknowledge)
    .then(handled => {
      if (!handled) {
        check.resume()
      }
    })
    .finally(() => {
      check.pending.current = false
    })

  return true
}
