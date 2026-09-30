import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'vitest'
import { archTargetMapFrom, loadArchTargetMap, loadInstalledArchTargetMap } from './builder-arch-targets.mjs'

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

test('a mismatched pin throws before the factory module runs', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'arch-pin-'))
  const marker = path.join(root, 'marker')
  let required = false
  assert.throws(
    () => loadInstalledArchTargetMap({
      resolve: () => path.join(root, 'package.json'),
      readFile: () => JSON.stringify({ name: 'app-builder-lib', version: '27.0.0-alpha.6', main: '../../../outside/index.js' }),
      require: () => {
        required = true
        fs.writeFileSync(marker, 'loaded')
        return { computeArchToTargetNamesMap: () => 'escaped' }
      },
    }),
    /Revalidate/,
  )
  assert.equal(required, false)
  assert.equal(fs.existsSync(marker), false)
})

test('a main outside the package is rejected before require', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'arch-root-'))
  const marker = path.join(root, 'marker')
  let required = false
  assert.throws(
    () => loadInstalledArchTargetMap({
      resolve: () => path.join(root, 'package.json'),
      readFile: () => JSON.stringify({ name: 'app-builder-lib', version: '26.15.3', main: '../../../outside/index.js' }),
      require: () => {
        required = true
        fs.writeFileSync(marker, 'loaded')
        return { computeArchToTargetNamesMap: () => 'escaped' }
      },
    }),
    /escapes/,
  )
  assert.equal(required, false)
  assert.equal(fs.existsSync(marker), false)
})
