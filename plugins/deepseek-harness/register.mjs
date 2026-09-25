/** Register through DSH's own CLI, staging file dependencies on its home volume. */
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { runProcess } from './bootstrap.mjs';

const bundle = dirname(fileURLToPath(import.meta.url));

export async function stageBundle(home, source = bundle) {
  const pkg = JSON.parse(await readFile(join(source, 'package.json'), 'utf8'));
  const files = [...new Set(['package.json', ...pkg.files])].sort();
  const payload = new Map();
  const digest = createHash('sha256');
  for (const file of files) {
    const absolute = resolve(source, file);
    const local = relative(source, absolute);
    if (isAbsolute(local) || local === '..' || local.startsWith('..' + sep) || !local) throw new Error('Invalid bundle file path');
    const bytes = await readFile(absolute);
    payload.set(file, bytes);
    digest.update(file).update('\0').update(bytes);
  }
  const hash = digest.digest('hex');
  const destination = join(resolve(home), 'one-search-bundles', hash.slice(0, 20));
  await mkdir(destination, { recursive: true });
  for (const [file, bytes] of payload) {
    const path = join(destination, file);
    await mkdir(dirname(path), { recursive: true });
    try { await writeFile(path, bytes, { flag: 'wx', mode: 0o600 }); }
    catch (error) {
      if (error.code !== 'EEXIST' || !bytes.equals(await readFile(path))) throw new Error('Staged bundle differs from expected content; choose another DSH home or repair the managed staging directory');
    }
  }
  return { directory: destination, sha256: hash, version: pkg.version };
}

export async function registerBundle(request, run = runProcess) {
  if (typeof request.dshPackage !== 'string' || !request.dshPackage) throw new Error('dshPackage is required');
  const profile = request.profile || 'web';
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(profile)) throw new Error('Invalid profile name');
  const home = resolve(request.dshHome || process.env.DSH_HOME || join(homedir(), '.dsh'));
  const staged = await stageBundle(home);
  // This process is a dedicated registration command; only its child inherits the home.
  const previous = process.env.DSH_HOME;
  try {
    process.env.DSH_HOME = home;
    const args = [join(resolve(request.dshPackage), 'lib', 'bin.js'), 'plugin', '--profile', profile,
      'add', 'file:' + staged.directory, '--ignore-scripts', '--registry=https://registry.npmjs.org'];
    if (request.offline === true) args.push('--offline');
    await run(process.execPath, args, { timeoutMs: 120000 });
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previous;
  }
  return { schema_version: 1, registered: true, connected: false, profile, dsh_home: home,
    bundle: staged, next_action: 'Start the selected DSH profile and call index_status/search' };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    if (!process.argv[2]) throw new Error('Pass a UTF-8 request JSON path');
    console.log(JSON.stringify(await registerBundle(JSON.parse(await readFile(process.argv[2], 'utf8')))));
  } catch (error) {
    console.log(JSON.stringify({ schema_version: 1, registered: false, connected: false,
      error: { code: 'dsh_registration_failed', type: error.name,
        message: 'Check dshPackage/profile, writable DSH home and package network/cache, then retry the same request' } }));
    process.exitCode = 1;
  }
}
