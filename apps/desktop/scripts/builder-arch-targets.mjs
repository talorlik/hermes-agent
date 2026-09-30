// electron-builder 27 exposes computeArchToTargetNamesMap at
// app-builder-lib/internal. This fork pins 26.15.3, which has no internal
// export. afterPack imports the Windows signer on every platform, so that
// missing path fails mac packaging before any target runs. Load the function
// from the installed compiled root. Reject any other pin, and reject a main
// that leaves the package, before requiring the factory.
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'

export const BUILDER_PIN = '26.15.3'

/**
 * @param {string} packageRoot
 * @param {{ name?: string, version?: string, main?: string }} manifest
 * @returns {string}
 */
export function factoryPathFor(packageRoot, manifest) {
  if (manifest.name !== 'app-builder-lib' || manifest.version !== BUILDER_PIN) {
    throw new Error(`Revalidate arch target map for ${manifest.name}@${manifest.version}`)
  }
  if (typeof manifest.main !== 'string' || manifest.main === '') {
    throw new Error('app-builder-lib 26.15.3 has no main')
  }
  const root = path.resolve(packageRoot)
  const compiled = path.resolve(root, path.dirname(manifest.main))
  const factoryPath = path.resolve(compiled, 'targets/targetFactory.js')
  for (const candidate of [compiled, factoryPath]) {
    const rel = path.relative(root, candidate)
    if (rel.startsWith('..') || path.isAbsolute(rel)) {
      throw new Error('arch target factory escapes the package root')
    }
  }
  return factoryPath
}

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

/**
 * @param {{ resolve: (spec: string) => string, readFile: (file: string) => string, require: (file: string) => { computeArchToTargetNamesMap?: unknown } }} io
 * @returns {Function}
 */
export function loadInstalledArchTargetMap(io) {
  const manifestPath = io.resolve('app-builder-lib/package.json')
  const packageRoot = path.dirname(manifestPath)
  const manifest = JSON.parse(io.readFile(manifestPath))
  const factoryPath = factoryPathFor(packageRoot, manifest)
  return archTargetMapFrom(manifest, io.require(factoryPath))
}

/** @param {string} fromFile @returns {Function} */
export function loadArchTargetMap(fromFile) {
  const require = createRequire(fromFile)
  return loadInstalledArchTargetMap({
    resolve: (spec) => require.resolve(spec),
    readFile: (file) => fs.readFileSync(file, 'utf8'),
    require,
  })
}
