// Loaded in the builder child before osx-sign. Keep its walk and classifier,
// but own each probe through close: isbinaryfile's path API resolves too early.
// The prebuilder lifecycle also imports this file to validate the supplier pins.
// electron-builder 26.15.3 resolves @electron/osx-sign 1.3.3 (dist/cjs) and
// isbinaryfile 4.0.10. Upstream's adapter pins 2.4.0/5.0.7, which ship with the
// 27 alpha this fork does not use. A supplier upgrade must fail here, not sign
// with a stale probe cap.
import fs from 'node:fs'
import path from 'node:path'
import Module, { createRequire, registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)

function packageVersion(entry) {
  let dir = path.dirname(entry)
  while (true) {
    const candidate = path.join(dir, 'package.json')
    if (fs.existsSync(candidate)) {
      return JSON.parse(fs.readFileSync(candidate, 'utf8')).version
    }
    const parent = path.dirname(dir)
    if (parent === dir) throw new Error(`no package.json above ${entry}`)
    dir = parent
  }
}

const signerEntry = require.resolve('@electron/osx-sign')
const signerRequire = createRequire(signerEntry)
const binaryEntry = signerRequire.resolve('isbinaryfile')
const signerVersion = packageVersion(signerEntry)
const binaryVersion = packageVersion(binaryEntry)
if (signerVersion !== '1.3.3' || binaryVersion !== '4.0.10') {
  throw new Error('Revalidate signing probe ownership for the installed osx-sign/isbinaryfile versions')
}

const utilPath = path.join(path.dirname(signerEntry), 'util.js')
const utilUrl = pathToFileURL(utilPath).href
const needle = `async function getFilePathIfBinary(filePath) {
    if (await (0, isbinaryfile_1.isBinaryFile)(filePath)) {
        return filePath;
    }
    return null;
}`
const replacement = `// Shared by concurrent walks in this builder child, never by unrelated fs users.
const probeWaiters = [];
let activeProbes = 0;
async function getFilePathIfBinary(filePath) {
    // Sixteen complete probes leave room for Node and codesign at a 64-fd limit.
    if (activeProbes >= 16) await new Promise(resolve => probeWaiters.push(resolve));
    else activeProbes++;
    try {
        const nodeFs = require('node:fs');
        const stat = await nodeFs.promises.stat(filePath);
        if (!stat.isFile()) throw new Error('Path provided was not a file!');
        const file = await nodeFs.promises.open(filePath, 'r');
        try {
            // Match isbinaryfile 4.0.10's 512-byte sample.
            const buffer = Buffer.alloc(512);
            const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
            return await isbinaryfile_1.isBinaryFile(buffer, bytesRead) ? filePath : null;
        } finally {
            await file.close();
        }
    } finally {
        const next = probeWaiters.shift();
        if (next) next();
        else activeProbes--;
    }
}`

function transform(source, urlOrPath) {
  if (!source.includes(needle)) {
    throw new Error(`osx-sign binary probe shape changed; revalidate the signing adapter (${urlOrPath})`)
  }
  return source.replace(needle, replacement)
}

// import() of the CJS util (the signing test, and any ESM load of that URL).
registerHooks({
  load(url, context, nextLoad) {
    const loaded = nextLoad(url, context)
    if (url !== utilUrl) return loaded
    return { ...loaded, source: transform(loaded.source.toString(), url) }
  }
})

// require() from osx-sign's CJS entry does not pass through the ESM load hook.
// Compile that one file from transformed source. Do not patch fs.open.
const originalJs = Module._extensions['.js']
Module._extensions['.js'] = function (module, filename) {
  if (path.resolve(filename) !== path.resolve(utilPath)) return originalJs(module, filename)
  const source = fs.readFileSync(filename, 'utf8')
  module._compile(transform(source, filename), filename)
}
