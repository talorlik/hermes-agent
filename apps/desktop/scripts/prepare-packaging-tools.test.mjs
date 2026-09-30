import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { test } from 'vitest'
import { builderCompiledRoot, pinnedPackageRoot } from './prepare-packaging-tools.mjs'

test('electron-builder 26.15.3 loads app-builder-lib from out, not dist', () => {
  const source = path.resolve(import.meta.dirname, '../../..')
  const root = pinnedPackageRoot(source, 'app-builder-lib')
  const compiled = builderCompiledRoot(root)
  assert.equal(path.basename(compiled), 'out')
  assert.equal(fs.existsSync(path.join(compiled, 'util', 'electronGet.js')), true)
  assert.equal(fs.existsSync(path.join(root, 'dist', 'util', 'electronGet.js')), false)
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))
  assert.equal(manifest.version, '26.15.3')
})
