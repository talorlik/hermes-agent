import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'
import { adaptConfigForBuilder, installedBuilderVersion, toolsetOverridesForBuilder, writeAdaptedConfig } from './builder-config-26.mjs'

const app = path.resolve(import.meta.dirname, '..')
const require = createRequire(path.join(app, 'package.json'))

test('26.15.3 config drops msix, flattens asar.unpack, and omits toolset file URLs', () => {
  assert.equal(installedBuilderVersion(app), '26.15.3')
  const source = require(path.join(app, 'electron-builder.config.cjs'))
  const adapted = adaptConfigForBuilder(source, '26.15.3')
  assert.equal('msix' in adapted, false)
  assert.equal(adapted.asar, true)
  assert.ok(adapted.asarUnpack.includes('**/*.node'))
  assert.ok(adapted.asarUnpack.includes('dist/**'))
  assert.deepEqual(toolsetOverridesForBuilder('26.15.3'), [])
  assert.throws(() => adaptConfigForBuilder(source, '27.0.0-alpha.6'), /Revalidate/)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'builder-config-'))
  const file = writeAdaptedConfig(path.join(app, 'electron-builder.config.cjs'), '26.15.3', dir)
  const loaded = require(file)
  assert.equal(loaded.asar, true)
  assert.equal('msix' in loaded, false)
  assert.ok(loaded.asarUnpack.includes('**/*.node'))
})
