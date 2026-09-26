/** Read-only probes against an authenticated, isolated DSH Web origin. */
import assert from 'node:assert/strict';
import { request as httpRequest } from 'node:http';

export async function verifyWebBoundaries(origin, cookie) {
  const body = JSON.stringify({ type: 'client-request', rpcId: 'boundary-check', method: 'request',
    payload: { action: 'status', params: {} } });
  const headers = { Cookie: cookie, Origin: origin, 'Content-Type': 'application/json' };
  const send = (path, bytes, extra = {}) => fetch(origin + path, { method: 'POST', headers: { ...headers, ...extra }, body: bytes });
  const report = {};
  for (const [key, path, bytes, expected] of [
    ['malformed_json', '/one-search/request', '{"unfinished":', 400],
    ['invalid_envelope', '/one-search/request', '{}', 400],
    ['wrong_method_envelope', '/one-search/request', body.replace('"method":"request"', '"method":"_stop"'), 400],
    ['wrong_path', '/one-search/other', body, 404],
    ['oversize_declared', '/one-search/request', 'x'.repeat(140000), 413],
  ]) {
    const response = await send(path, bytes);
    assert.equal(response.status, expected, key);
    await response.arrayBuffer();
    report[key] = response.status;
  }
  const wrongVerb = await fetch(origin + '/one-search/request', { headers });
  assert.equal(wrongVerb.status, 405); await wrongVerb.arrayBuffer(); report.wrong_http_method = wrongVerb.status;
  const crossSite = await send('/one-search/request', body, { 'Sec-Fetch-Site': 'cross-site' });
  assert.equal(crossSite.status, 403); await crossSite.arrayBuffer(); report.cross_site = crossSite.status;
  // Node fetch normalizes Host to its URL; use node:http to send the actual
  // hostile authority a rebinding request would put on the wire.
  report.untrusted_host = await new Promise((fulfill, reject) => {
    const req = httpRequest(new URL('/one-search/request', origin), { method: 'POST',
      headers: { ...headers, Host: 'untrusted.example' } }, (response) => {
      response.resume(); response.on('end', () => fulfill(response.statusCode)); response.on('error', reject);
    });
    req.setTimeout(5000, () => req.destroy(new Error('Host boundary probe timed out')));
    req.on('error', reject); req.end(body);
  });
  assert.equal(report.untrusted_host, 403);
  // No Content-Length: the handler must enforce its cap on streamed bytes too.
  report.oversize_chunked = await new Promise((fulfill, reject) => {
    const req = httpRequest(new URL('/one-search/request', origin), { method: 'POST', headers }, (response) => {
      response.resume();
      response.on('end', () => fulfill(response.statusCode));
      response.on('error', reject);
    });
    req.setTimeout(5000, () => req.destroy(new Error('Chunked boundary probe timed out')));
    req.on('error', reject);
    for (let n = 0; n < 14; n++) req.write('x'.repeat(10000));
    req.end();
  });
  assert.equal(report.oversize_chunked, 413);
  const largeApplication = JSON.stringify({ type: 'client-request', rpcId: 'boundary-check', method: 'request',
    payload: { action: 'settings_save', params: { padding: 'x'.repeat(128 * 1024) } } });
  assert.ok(Buffer.byteLength(largeApplication) < 129 * 1024);
  const applicationResponse = await send('/one-search/request', largeApplication);
  assert.equal(applicationResponse.status, 200);
  const application = await applicationResponse.json();
  assert.equal(application.result.value.error.code, 'invalid_request');
  report.application_size_limit_enforced = true;
  for (const action of ['_stop', 'raw_sql', 'execute']) {
    const response = await send('/one-search/request', body.replace('"action":"status"', '"action":' + JSON.stringify(action)));
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.result.value.ok, false);
    assert.equal(result.result.value.error.code, 'invalid_request');
  }
  report.arbitrary_actions_rejected = true;
  const healthy = await (await send('/one-search/request', body)).json();
  assert.equal(healthy.result.value.result.service.status, 'running');
  report.service_remains_running = true;
  return report;
}
