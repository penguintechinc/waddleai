// @vitest-environment node
/**
 * Smoke/integration tests for the Express static server (server.js).
 * Boots the real server against a fixture dist/ and a real upstream stub —
 * no mocks of the proxy path, so a dead /api route fails loudly.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import net from 'node:net'
import zlib from 'node:zlib'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { createServer, SECURITY_HEADERS } = require('./server.js')

const INDEX_HTML = '<!doctype html><html><body><div id="root">waddleai-spa</div></body></html>'

/** Issues a request and resolves with status, headers and raw body buffer. */
function request(port, { method = 'GET', url = '/', headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: url, headers }, (res) => {
      const chunks = []
      res.on('data', (c) => chunks.push(c))
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }))
    })
    req.on('error', reject)
    if (body) req.write(body)
    req.end()
  })
}

/** Listens on an ephemeral port and resolves with the bound port. */
function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)))
}

/** Returns a port nothing is listening on (bind, read, release). */
async function closedPort() {
  const s = net.createServer()
  const port = await listen(s)
  await new Promise((r) => s.close(r))
  return port
}

describe('webui express server', () => {
  let distDir
  let upstream
  let upstreamPort
  let seen
  let app
  let port
  let deadApp
  let deadPort

  beforeAll(async () => {
    distDir = fs.mkdtempSync(path.join(os.tmpdir(), 'webui-dist-'))
    fs.mkdirSync(path.join(distDir, 'assets'))
    fs.writeFileSync(path.join(distDir, 'index.html'), INDEX_HTML)
    fs.writeFileSync(path.join(distDir, 'assets', 'index-abc123.js'), `console.log("x");${'a'.repeat(4096)}`)
    fs.writeFileSync(path.join(distDir, 'logo.png'), 'png-bytes')

    upstream = http.createServer((req, res) => {
      seen = { url: req.url, headers: req.headers }
      // Hostile upstream: tries to replace our CSP and set a cookie.
      res.setHeader('Content-Security-Policy', "default-src *")
      res.setHeader('Set-Cookie', 'sid=abc; HttpOnly')
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify({ ok: true }))
    })
    upstream.on('upgrade', (req, socket) => {
      socket.write(
        'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nws-upstream-ok',
      )
      socket.end()
    })
    upstreamPort = await listen(upstream)

    app = createServer({ distDir, apiUpstream: `http://127.0.0.1:${upstreamPort}` })
    port = await listen(app)

    deadApp = createServer({ distDir, apiUpstream: `http://127.0.0.1:${await closedPort()}` })
    deadPort = await listen(deadApp)
  })

  afterAll(async () => {
    for (const s of [app, deadApp, upstream]) await new Promise((r) => s.close(r))
    fs.rmSync(distDir, { recursive: true, force: true })
  })

  it('serves index.html at / without caching it', async () => {
    const res = await request(port)
    expect(res.status).toBe(200)
    expect(res.body.toString()).toBe(INDEX_HTML)
    expect(res.headers['cache-control']).toBe('no-cache')
  })

  it('sets all five security headers with exact values on every response kind', async () => {
    const responses = await Promise.all([
      request(port),
      request(port, { url: '/assets/index-abc123.js' }),
      request(port, { url: '/some/spa/route' }),
      request(port, { url: '/assets/missing.js' }),
      request(port, { url: '/health' }),
      request(port, { url: '/api/v1/ping' }),
      request(deadPort, { url: '/api/v1/ping' }),
    ])
    expect(responses).toHaveLength(7)
    expect(Object.keys(SECURITY_HEADERS)).toHaveLength(5)
    for (const res of responses) {
      for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
        expect(res.headers[name.toLowerCase()]).toBe(value)
      }
      expect(res.headers['x-xss-protection']).toBeUndefined()
      expect(res.headers['x-powered-by']).toBeUndefined()
    }
    expect(SECURITY_HEADERS['Content-Security-Policy']).toBe(
      "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; " +
        "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'",
    )
  })

  it('gives hashed assets immutable 1y cache with Expires', async () => {
    for (const url of ['/assets/index-abc123.js', '/logo.png']) {
      const res = await request(port, { url })
      expect(res.status).toBe(200)
      expect(res.headers['cache-control']).toBe('public, max-age=31536000, immutable')
      expect(Date.parse(res.headers.expires)).toBeGreaterThan(Date.now() + 360 * 24 * 3600 * 1000)
    }
  })

  it('gzips compressible responses over 1KiB when requested', async () => {
    const res = await request(port, { url: '/assets/index-abc123.js', headers: { 'accept-encoding': 'gzip' } })
    expect(res.headers['content-encoding']).toBe('gzip')
    expect(zlib.gunzipSync(res.body).toString()).toContain('console.log("x")')
  })

  it('serves index.html for unknown SPA routes (no-cache)', async () => {
    const res = await request(port, { url: '/dashboard/teams/42?tab=keys' })
    expect(res.status).toBe(200)
    expect(res.body.toString()).toBe(INDEX_HTML)
    expect(res.headers['cache-control']).toBe('no-cache')
  })

  it('404s missing assets instead of returning index.html', async () => {
    for (const url of ['/assets/missing-deadbeef.js', '/nope.css', '/favicon.ico']) {
      const res = await request(port, { url })
      expect(res.status).toBe(404)
      expect(res.body.toString()).not.toContain('waddleai-spa')
    }
  })

  it('404s non-GET requests that reach the SPA fallback', async () => {
    const res = await request(port, { method: 'POST', url: '/dashboard' })
    expect(res.status).toBe(404)
  })

  it('returns healthy text on /health', async () => {
    const res = await request(port, { url: '/health' })
    expect(res.status).toBe(200)
    expect(res.body.toString()).toBe('healthy\n')
    expect(res.headers['content-type']).toMatch(/^text\/plain/)
  })

  it('does not write an access-log line for /health', async () => {
    const lines = []
    const orig = process.stdout.write.bind(process.stdout)
    process.stdout.write = (chunk, ...rest) => {
      lines.push(String(chunk))
      return orig(chunk, ...rest)
    }
    try {
      await request(port, { url: '/health' })
      await request(port, { url: '/log-probe' })
      await new Promise((r) => setTimeout(r, 50))
    } finally {
      process.stdout.write = orig
    }
    const logged = lines.join('')
    expect(logged).toContain('"path":"/log-probe"')
    expect(logged).not.toContain('"path":"/health"')
  })

  it('proxies /api to upstream, not the SPA fallback', async () => {
    const res = await request(port, {
      url: '/api/v1/auth/me?x=1',
      headers: {
        cookie: 'sid=client-cookie',
        'x-requested-with': 'XMLHttpRequest',
        'x-forwarded-for': '203.0.113.9',
        'x-forwarded-proto': 'https',
      },
    })
    expect(res.status).toBe(200)
    expect(JSON.parse(res.body.toString())).toEqual({ ok: true })
    expect(seen.url).toBe('/api/v1/auth/me?x=1')
    expect(seen.headers.cookie).toBe('sid=client-cookie')
    expect(seen.headers['x-requested-with']).toBe('XMLHttpRequest')
    expect(seen.headers.host).toBe(`127.0.0.1:${port}`)
    expect(seen.headers['x-real-ip']).toBe('127.0.0.1')
    expect(seen.headers['x-forwarded-for']).toBe('203.0.113.9,127.0.0.1')
    expect(seen.headers['x-forwarded-proto']).toBe('https')
    expect(res.headers['set-cookie']).toEqual(['sid=abc; HttpOnly'])
  })

  it('overwrites a spoofed X-Real-IP', async () => {
    await request(port, { url: '/api/x', headers: { 'x-real-ip': '6.6.6.6' } })
    expect(seen.headers['x-real-ip']).toBe('127.0.0.1')
  })

  it('does not let upstream override our CSP', async () => {
    const res = await request(port, { url: '/api/v1/ping' })
    expect(res.headers['content-security-policy']).toBe(SECURITY_HEADERS['Content-Security-Policy'])
  })

  it('returns 502 JSON (never index.html) when the API upstream is unreachable', async () => {
    const res = await request(deadPort, { url: '/api/v1/ping' })
    expect(res.status).toBe(502)
    expect(JSON.parse(res.body.toString()).error).toBe('bad_gateway')
    const bare = await request(deadPort, { url: '/api' })
    expect(bare.status).toBe(502)
  })

  it('proxies POST bodies to upstream', async () => {
    const res = await request(port, {
      method: 'POST',
      url: '/api/v1/items',
      headers: { 'content-type': 'application/json', 'content-length': '2' },
      body: '{}',
    })
    expect(res.status).toBe(200)
  })

  it('does not treat /apiary as an API path', async () => {
    const res = await request(deadPort, { url: '/apiary' })
    expect(res.status).toBe(200)
    expect(res.body.toString()).toBe(INDEX_HTML)
  })

  /** Sends a raw WebSocket upgrade and resolves with everything received. */
  function wsUpgrade(targetPort, url) {
    return new Promise((resolve) => {
      const sock = net.connect(targetPort, '127.0.0.1', () => {
        sock.write(
          `GET ${url} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
            'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n',
        )
      })
      let data = ''
      sock.on('data', (d) => (data += d))
      sock.on('close', () => resolve(data))
      sock.on('error', () => resolve(data))
    })
  }

  it('proxies WebSocket upgrades on /api and rejects them elsewhere', async () => {
    const proxied = await wsUpgrade(port, '/api/v1/stream')
    expect(proxied).toContain('101 Switching Protocols')
    expect(proxied).toContain('ws-upstream-ok')
    const rejected = await wsUpgrade(port, '/socket')
    expect(rejected).toBe('')
  })
})
