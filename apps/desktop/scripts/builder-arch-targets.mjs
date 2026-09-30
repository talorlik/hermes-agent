// electron-builder 27 exposes computeArchToTargetNamesMap at
// app-builder-lib/internal. This fork pins 26.15.3, which has no internal
// export. afterPack imports the Windows signer on every platform, so that
// missing path fails mac packaging before any target runs. Load the function
// from the installed compiled root, and fail closed on any other pin.
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'

export const BUILDER_PIN = '26.15.3'

/**
 * @param {{ name?: string, version?: string }} manifest
 * @param {{ computeArchToTargetNamesMap?: unknown }} factory
 * @returns {Function}
 */
export function archTargetMapFrom(manifest, factory) {
  if (manifest.name !== 'app-builder-lib' || manifest.version !== BUILDER_PIN) {
    throw new Error(`Revalidate arch target map for ${manifest.name}@${manifest.version}`)
  }
  if (typeof factory.computeArchToTargetNamesMap !== 'function') {
    throw new Error('app-builder-lib 26.15.3 is missing computeArchToTargetNamesMap')
  }
  return factory.computeArchToTargetNamesMap
}

/** @param {string} fromFile @returns {Function} */
export function loadArchTargetMap(fromFile) {
  const require = createRequire(fromFile)
  const manifest = JSON.parse(fs.readFileSync(require.resolve('app-builder-lib/package.json'), 'utf8'))
  const compiled = path.dirname(require.resolve('app-builder-lib'))
  return archTargetMapFrom(manifest, require(path.join(compiled, 'targets/targetFactory.js')))
}
