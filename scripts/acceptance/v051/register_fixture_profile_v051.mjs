/** Stage only a new isolated profile; do not start its host or runtime. */
import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
const here = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '.packaging-smoke');
const root = resolve(request.fixtureRoot);
const part = relative(here, root);
assert.ok(part.startsWith('upgrade-v051-') && !isAbsolute(part) && !part.startsWith('..' + sep));
const home = resolve(request.home);
const homePart = relative(root, home);
assert.ok(homePart && !isAbsolute(homePart) && !homePart.startsWith('..' + sep));
try { await access(home); throw new Error('Only a fresh isolated profile may be staged'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
process.env.NODE_OPTIONS = '--max-old-space-size=192 --max-semi-space-size=4';
process.env.DSH_TELEMETRY_DISABLED = '1';
const { registerBundle } = await import(pathToFileURL(join(resolve(request.bundleDir), 'register.mjs')));
const result = await registerBundle({ dshPackage: request.dshPackage, dshHome: home, profile: 'web', offline: request.offline === true });
console.log(JSON.stringify({ staged: true, connected: false, bundle_sha256: result.bundle.sha256 }));
