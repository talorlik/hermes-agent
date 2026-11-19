// Fork-only (G-DESKTOP-TEST-ISOLATION). ensureGitRepo passes `-c core.hooksPath=`
// to its synthetic bootstrap commit; this file holds that proof so the upstream
// test module next to it stays byte-identical to upstream.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { ensureGitRepo } from './git-worktree-ops'

test('ensureGitRepo: ignores user commit hooks for the synthetic root commit', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-wt-hooks-'))
  const hooks = path.join(dir, 'hooks')
  const globalConfig = path.join(dir, 'global.gitconfig')
  const previousGlobalConfig = process.env.GIT_CONFIG_GLOBAL

  try {
    fs.mkdirSync(hooks)
    fs.writeFileSync(path.join(hooks, 'commit-msg'), '#!/bin/sh\nexit 1\n')
    fs.chmodSync(path.join(hooks, 'commit-msg'), 0o755)
    fs.writeFileSync(globalConfig, `[core]\n\thooksPath = ${hooks}\n`)
    process.env.GIT_CONFIG_GLOBAL = globalConfig

    await ensureGitRepo('git', dir)

    const head = execFileSync('git', ['-C', dir, 'rev-parse', '--verify', 'HEAD']).toString().trim()
    assert.match(head, /^[0-9a-f]{7,}$/)
  } finally {
    if (previousGlobalConfig === undefined) {
      delete process.env.GIT_CONFIG_GLOBAL
    } else {
      process.env.GIT_CONFIG_GLOBAL = previousGlobalConfig
    }

    fs.rmSync(dir, { recursive: true, force: true })
  }
})
