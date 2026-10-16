'use strict'

/**
 * WaddleAI WebUI static server.
 *
 * Serves the Vite build output (dist/) and reverse-proxies /api to the
 * management service, replacing the previous nginx container. It is the
 * house-standard "Express serves all static files + /api proxy" shape.
 * Writes nothing to disk, so it runs under a read-only root filesystem.
 */

const path = require('node:path')
const http = require('node:http')
const express = require('express')
const compression = require('compression')
const httpProxy = require('http-proxy')

const DEFAULT_API_UPSTREAM = 'http://waddleai-management.waddleai.svc.cluster.local:8001'
const ONE_YEAR_SECONDS = 365 * 24 * 60 * 60

/**
 * Security headers sent on every response. Values match the retired nginx
 * `add_header ... always` set exactly. X-XSS-Protection is deliberately
 * absent (obsolete and harmful in legacy browsers) — do not add it back.
 */
const SECURITY_HEADERS = Object.freeze({
  'X-Frame-Options': 'SAMEORIGIN',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'Content-Security-Policy':
    "default-src 'self'; base-uri 'self'; object-src 'none'; " +
    "frame-ancestors 'self'; form-action 'self'; script-src 'self'; " +
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
    "font-src 'self' data:; connect-src 'self'",
})

const ASSET_EXT = /\.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$/i

/** Reports whether a URL path targets the /api reverse proxy. */
function isApiPath(urlPath) {
  return urlPath === '/api' || urlPath.startsWith('/api/')
}

/** Writes one access-log line to stdout (path only, never the query string). */
function logRequest(req, res, startedAt) {
  const line = {
    ts: new Date().toISOString(),
    level: 'INFO',
    msg: 'http_request',
    method: req.method,
    path: req.path,
    status: res.statusCode,
    duration_ms: Date.now() - startedAt,
  }
  process.stdout.write(`${JSON.stringify(line)}\n`)
}

/** Writes a structured diagnostic line to stdout (stdout-only logging). */
function logEvent(level, msg, fields = {}) {
  process.stdout.write(
    `${JSON.stringify({ ts: new Date().toISOString(), level, msg, ...fields })}\n`,
  )
}

/**
 * Builds the reverse proxy for /api. Cookies and X-Requested-With pass
 * through untouched (the frontend's cookie auth + CSRF marker depend on
 * both); Host is preserved; X-Forwarded-* and X-Real-IP identify the client.
 */
function createApiProxy(target) {
  const proxy = httpProxy.createProxyServer({
    target,
    ws: true,
    xfwd: true,
    changeOrigin: false,
  })

  proxy.on('proxyReq', (proxyReq, req) => {
    proxyReq.setHeader('X-Real-IP', req.socket.remoteAddress || '')
    // xfwd appends the hop's scheme to any incoming value ("https,http");
    // keep only the original client-facing scheme at the head of the list.
    const proto = String(proxyReq.getHeader('x-forwarded-proto') || '').split(',')[0].trim()
    if (proto) proxyReq.setHeader('X-Forwarded-Proto', proto)
  })

  // Upstream must never be able to weaken or drop our security headers.
  proxy.on('proxyRes', (proxyRes) => {
    for (const name of Object.keys(SECURITY_HEADERS)) {
      delete proxyRes.headers[name.toLowerCase()]
    }
  })

  proxy.on('error', (err, req, res) => {
    logEvent('ERROR', 'api_proxy_error', { code: err.code || err.name, path: req.url?.split('?')[0] })
    if (res && typeof res.writeHead === 'function' && !res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: 'bad_gateway', detail: 'API upstream unavailable' }))
    } else if (res && typeof res.destroy === 'function') {
      res.destroy() // websocket upgrade: res is the raw socket
    }
  })

  return proxy
}

/**
 * Creates the Express app and the /api proxy. Returns both so the caller can
 * wire the proxy into the HTTP server's `upgrade` event for WebSockets.
 */
function createApp({ distDir, apiUpstream } = {}) {
  const root = distDir || process.env.WEBUI_DIST_DIR || path.join(__dirname, 'dist')
  const target = apiUpstream || process.env.API_UPSTREAM || DEFAULT_API_UPSTREAM
  const indexPath = path.join(root, 'index.html')
  const proxy = createApiProxy(target)

  const app = express()
  app.disable('x-powered-by')

  // Security headers first, so every response carries them (health, proxy
  // errors, 404s) — the `always` semantics of the old nginx config.
  app.use((req, res, next) => {
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) res.setHeader(name, value)
    next()
  })

  app.get('/health', (req, res) => {
    res.type('text/plain').status(200).send('healthy\n')
  })

  app.use((req, res, next) => {
    const startedAt = Date.now()
    res.on('finish', () => logRequest(req, res, startedAt))
    next()
  })

  // /api is mounted before static + SPA fallback so it can never fall through
  // to index.html. Not compressed here: upstream owns its own encoding.
  app.use((req, res, next) => {
    if (!isApiPath(req.path)) return next()
    return proxy.web(req, res)
  })

  app.use(compression({ threshold: 1024 }))

  app.use(
    express.static(root, {
      index: 'index.html',
      cacheControl: false,
      setHeaders(res, filePath) {
        if (ASSET_EXT.test(filePath)) {
          res.setHeader('Cache-Control', 'public, max-age=31536000, immutable')
          res.setHeader('Expires', new Date(Date.now() + ONE_YEAR_SECONDS * 1000).toUTCString())
        } else {
          // index.html (and anything else) must be revalidated so a new
          // deploy's hashed bundle names are picked up immediately.
          res.setHeader('Cache-Control', 'no-cache')
        }
      },
    }),
  )

  // SPA fallback: only for navigations — a missing asset must 404 rather
  // than return index.html with 200 (which breaks module loading opaquely).
  app.use((req, res, next) => {
    const isAsset = req.path.startsWith('/assets/') || ASSET_EXT.test(req.path)
    if ((req.method !== 'GET' && req.method !== 'HEAD') || isAsset) {
      return res.status(404).type('text/plain').send('Not Found\n')
    }
    return res.sendFile(indexPath, { headers: { 'Cache-Control': 'no-cache' } }, (err) => {
      if (err) next(err)
    })
  })

  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    logEvent('ERROR', 'unhandled_error', { name: err.name, path: req.path })
    if (res.headersSent) return res.destroy()
    return res.status(500).type('text/plain').send('Internal Server Error\n')
  })

  return { app, proxy }
}

/** Creates the HTTP server with WebSocket upgrades routed to the /api proxy. */
function createServer(options) {
  const { app, proxy } = createApp(options)
  const server = http.createServer(app)
  server.on('upgrade', (req, socket, head) => {
    if (isApiPath((req.url || '').split('?')[0])) {
      proxy.ws(req, socket, head)
    } else {
      socket.destroy()
    }
  })
  return server
}

if (require.main === module) {
  const port = Number(process.env.PORT || 8080)
  const server = createServer()
  server.listen(port, () => logEvent('INFO', 'webui_listening', { port }))
  const shutdown = () => server.close(() => process.exit(0))
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

module.exports = { createApp, createServer, SECURITY_HEADERS, DEFAULT_API_UPSTREAM }
