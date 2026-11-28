import { spawn } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { expect, test } from 'vitest'

import { desktopMainProcesses, type InstallFacts, installProcesses, releaseDir } from './harness'

// Electron 44 zeroes its processes' /proc/<pid>/environ, so the installed app shows no
// HERMES_HOME. A copy of node under the install's release dir, started with an empty
// environment, is that process.
test.skipIf(process.platform !== 'linux')(
  'the install census still sees the packaged app when its environment names no HERMES_HOME',
  async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'update census '))

    const facts = {
      hermesHome: path.join(root, 'home', '.hermes'),
      checkout: path.join(root, 'checkout')
    } as InstallFacts

    fs.mkdirSync(releaseDir(facts), { recursive: true })
    const exe = path.join(releaseDir(facts), 'Hermes')
    fs.copyFileSync(process.execPath, exe)
    const app = spawn(exe, ['-e', 'setTimeout(() => {}, 60_000)'], { env: {}, stdio: 'ignore' })

    try {
      await expect.poll(() => installProcesses(facts).map(proc => proc.pid)).toContain(app.pid)
      expect(desktopMainProcesses(facts).map(proc => proc.pid)).toEqual([app.pid])
    } finally {
      app.kill('SIGKILL')
      await expect.poll(() => installProcesses(facts).length).toBe(0)
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
)

test.skipIf(process.platform !== 'linux')(
  'selects the app, excluding its renderer and a detached crashpad executable',
  async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'update topology '))

    const facts = {
      hermesHome: path.join(root, 'home', '.hermes'),
      checkout: path.join(root, 'checkout')
    } as InstallFacts

    fs.mkdirSync(releaseDir(facts), { recursive: true })
    const exe = path.join(releaseDir(facts), 'Hermes')
    const crashpadExe = path.join(releaseDir(facts), 'chrome_crashpad_handler')
    fs.copyFileSync(process.execPath, exe)
    fs.copyFileSync(process.execPath, crashpadExe)
    const script = path.join(root, 'app.cjs')
    fs.writeFileSync(
      script,
      `
      const { spawn } = require('node:child_process')
      const renderer = spawn(process.execPath,
        ['-e', 'setInterval(() => {}, 1000)', '--', '--type=renderer'],
        { env: {}, stdio: 'ignore' })
      process.on('SIGTERM', () => renderer.kill('SIGTERM'))
      renderer.on('exit', () => process.exit())
    `
    )

    const crashpad = spawn(crashpadExe, ['-e', 'setInterval(() => {}, 1000)', '--', '--monitor-self'], {
      env: {},
      stdio: 'ignore',
      detached: true
    })

    const app = spawn(exe, [script], { env: {}, stdio: 'ignore' })

    try {
      await expect.poll(() => installProcesses(facts).length).toBe(3)
      const census = installProcesses(facts)
      expect(census.find(proc => proc.cmdline.includes('--type=renderer'))?.ppid).toBe(app.pid)
      expect(census.find(proc => proc.pid === crashpad.pid)?.ppid).not.toBe(app.pid)
      expect(desktopMainProcesses(facts).map(proc => proc.pid)).toEqual([app.pid])
    } finally {
      app.kill('SIGTERM')
      crashpad.kill('SIGTERM')
      await expect.poll(() => installProcesses(facts).length).toBe(0)
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
)
