// electron-builder 27 alpha accepts msix, asar.unpack, and toolsets.*.url.
// This fork pins 26.15.3. That schema rejects those three shapes. Adapt only
// that pin. A different builder version must fail here, not package with a
// stale translation.
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'

export const BUILDER_PIN = '26.15.3'

/**
 * @param {object} config
 * @param {string} version
 * @returns {object}
 */
export function adaptConfigForBuilder(config, version) {
  if (version !== BUILDER_PIN) {
    throw new Error(`Revalidate the builder config adapter for electron-builder ${version}`)
  }
  const next = { ...config }
  delete next.msix
  if (next.asar && typeof next.asar === 'object') {
    const unpack = next.asar.unpack
    if (unpack != null) {
      const extra = Array.isArray(unpack) ? unpack : [unpack]
      const existing = next.asarUnpack == null ? [] : (Array.isArray(next.asarUnpack) ? next.asarUnpack : [next.asarUnpack])
      next.asarUnpack = [...existing, ...extra]
    }
    next.asar = true
  }
  return next
}

/**
 * 26.15.3 ToolsetConfig is a set of version enums. File URLs are additional
 * properties and fail validation. The builder downloads its own 7zip and icons.
 * @param {string} version
 * @returns {string[]}
 */
export function toolsetOverridesForBuilder(version) {
  if (version !== BUILDER_PIN) {
    throw new Error(`Revalidate toolset overrides for electron-builder ${version}`)
  }
  return []
}

/** @param {string} sourceConfigPath @param {string} version @param {string} directory @returns {string} */
export function writeAdaptedConfig(sourceConfigPath, version, directory) {
  if (version !== BUILDER_PIN) {
    throw new Error(`Revalidate the builder config adapter for electron-builder ${version}`)
  }
  const file = path.join(directory, 'electron-builder.26.cjs')
  const body = `const config = require(${JSON.stringify(sourceConfigPath)})
delete config.msix
if (config.asar && typeof config.asar === 'object') {
  const unpack = config.asar.unpack
  if (unpack != null) {
    const extra = Array.isArray(unpack) ? unpack : [unpack]
    const existing = config.asarUnpack == null ? [] : (Array.isArray(config.asarUnpack) ? config.asarUnpack : [config.asarUnpack])
    config.asarUnpack = [...existing, ...extra]
  }
  config.asar = true
}
module.exports = config
`
  fs.writeFileSync(file, body)
  return file
}

export function installedBuilderVersion(app) {
  const require = createRequire(path.join(app, 'package.json'))
  const manifest = require.resolve('electron-builder/package.json')
  return JSON.parse(fs.readFileSync(manifest, 'utf8')).version
}
