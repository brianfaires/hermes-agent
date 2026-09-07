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
    if (result?.handled === false) return false
    acknowledge(result?.handled ? result.output || '(no output)' : 'Private command check failed; input was not submitted.')
  } catch {
    acknowledge('Private command check failed; input was not submitted.')
  }
  return true
}
