import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'vitest'
import { archTargetMapFrom, loadArchTargetMap } from './builder-arch-targets.mjs'

const scripts = import.meta.dirname
const app = path.resolve(scripts, '..')

test('mac afterPack loads on electron-builder 26.15.3 without app-builder-lib/internal', async () => {
  const hook = await import(pathToFileURL(path.join(scripts, 'after-pack.mjs')).href)
  assert.equal(typeof hook.default, 'function')
})

test('26.15.3 arch map is the installed target factory, and another pin fails closed', () => {
  const require = createRequire(path.join(app, 'package.json'))
  const compiled = path.dirname(require.resolve('app-builder-lib'))
  const installed = require(path.join(compiled, 'targets/targetFactory.js')).computeArchToTargetNamesMap
  assert.equal(loadArchTargetMap(path.join(app, 'package.json')), installed)
  assert.throws(
    () => archTargetMapFrom({ name: 'app-builder-lib', version: '27.0.0-alpha.6' }, { computeArchToTargetNamesMap() {} }),
    /Revalidate/,
  )
})
