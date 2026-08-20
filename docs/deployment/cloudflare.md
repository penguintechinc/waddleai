# Cloudflare Pages Deployment

> **Stale — does not reflect the current repo.** There is no `website/` directory in this repo (verified: `git log` shows a `/website` tree existed historically but it's gone from the current tree), so `website/wrangler.toml`, `website/.pages.yml`, and the build steps below don't apply as written. Per the current convention (see root `CLAUDE.md` "Website Integration Requirements"), the marketing site is a sparse-checkout submodule of `github.com/penguintechinc/website`, not a folder in this repo. The domain below (`waddlebot.ai`) also doesn't match any WaddleAI domain used elsewhere in this repo's docs and may be left over from an earlier product name. Left in place for reference only — verify against the actual `penguintechinc/website` repo before following any step here.

## Prerequisites

- GitHub repository with WaddleAI code
- Cloudflare account with domain management access
- Domain `waddlebot.ai` configured in Cloudflare

## Cloudflare Pages Setup

### 1. Create New Pages Project

1. **Login to Cloudflare Dashboard**
   - Go to [dash.cloudflare.com](https://dash.cloudflare.com)
   - Navigate to **Pages** section

2. **Connect GitHub Repository**
   - Click **"Create a project"**
   - Select **"Connect to Git"**
   - Choose **GitHub** and authorize Cloudflare
   - Select repository: `penguintechinc/waddleai`
   - Choose branch: `main` or `v1.x`

### 2. Configure Build Settings

#### Framework Preset
- **Framework preset**: `Next.js (Static HTML Export)`

#### Build Configuration
- **Build command**: `npm run build`
- **Build output directory**: `out`
- **Root directory**: `website`

#### Environment Variables
```bash
NODE_VERSION=18.17.0
NPM_VERSION=latest
NEXT_TELEMETRY_DISABLED=1
```

### 3. Domain Configuration

#### Custom Domain Setup
1. **Add Custom Domain**
   - In Pages project settings, go to **Custom domains**
   - Click **"Set up a custom domain"**
   - Enter: `waddlebot.ai`
   - Cloudflare will automatically configure DNS

2. **SSL/TLS Settings**
   - Encryption mode: **"Full (strict)"**
   - Always Use HTTPS: **Enabled**
   - HSTS: **Enabled**

#### DNS Records (Auto-configured)
```
Type: CNAME
Name: @
Target: waddleai-website.pages.dev
Proxy: Enabled (Orange cloud)
```

### 4. Build Configuration Files

The repository includes these Cloudflare-specific config files:

#### `website/wrangler.toml`
```toml
name = "waddleai-website"
compatibility_date = "2024-01-01"

[env.production]
name = "waddleai-website-production"

[build]
command = "npm run build"
cwd = "."

[[pages_build_output_dir]]
value = "out"
```

#### `website/.pages.yml`
```yaml
build:
  command: "npm run build"
  output_directory: "out"
  root_dir: "."

environment:
  NODE_VERSION: "18.x"
  NPM_VERSION: "latest"
```

### 5. Performance Optimization

#### Cache Configuration
Cloudflare Pages automatically handles caching for static assets:

- **Static Assets**: `/_next/static/*` cached for 1 year
- **HTML Pages**: Cached with smart invalidation
- **Images**: Optimized and cached globally

#### Security Headers
The following security headers are automatically applied:

```yaml
headers:
  - source: "/*"
    headers:
      - key: "X-Frame-Options"
        value: "DENY"
      - key: "X-Content-Type-Options"
        value: "nosniff"
      - key: "Referrer-Policy"
        value: "strict-origin-when-cross-origin"
```

### 6. Deployment Process

#### Automatic Deployments
- **Production**: Deploys on push to `main` branch
- **Preview**: Deploys on push to feature branches
- **Build time**: ~2-3 minutes
- **Global distribution**: ~1 minute

#### Manual Deployment
```bash
# Install Wrangler CLI
npm install -g wrangler

# Login to Cloudflare
wrangler login

# Deploy manually
cd website
wrangler pages deploy out --project-name waddleai-website
```

### 7. Monitoring & Analytics

#### Pages Analytics
- **Performance metrics**: Core Web Vitals tracking
- **Traffic analytics**: Page views, unique visitors
- **Geographic distribution**: Global traffic patterns

#### Build Monitoring
- **Build logs**: Available in Cloudflare dashboard
- **Build notifications**: Configure webhook alerts
- **Deployment status**: Monitor via API

### 8. Environment Management

#### Production Environment
```bash
# Environment: production
# Domain: waddlebot.ai
# Build: main branch
# Analytics: Full tracking enabled
```

#### Preview Environment
```bash
# Environment: preview
# Domain: <commit-hash>.waddleai-website.pages.dev
# Build: feature branches
# Analytics: Limited tracking
```

### 9. Troubleshooting

#### Common Build Issues

**Build Command Fails**
```bash
# Check Node.js version
NODE_VERSION=18.17.0

# Clear build cache
rm -rf .next out node_modules
npm install
npm run build
```

**Static Export Issues**
```bash
# Verify Next.js config
output: 'export'
trailingSlash: true
images: { unoptimized: true }
```

#### DNS Issues

**Domain Not Resolving**
1. Check CNAME record points to `*.pages.dev`
2. Ensure Cloudflare proxy is enabled (orange cloud)
3. Verify SSL/TLS encryption mode is "Full (strict)"

### 10. Best Practices

#### Performance
- ✅ Use `next/image` with `unoptimized: true`
- ✅ Implement proper meta tags for SEO
- ✅ Minimize bundle size with tree shaking
- ✅ Use Cloudflare's global CDN benefits

#### Security
- ✅ Enable HSTS and security headers
- ✅ Use CSP headers for XSS protection
- ✅ Configure proper CORS policies
- ✅ Regular dependency updates

#### Monitoring
- ✅ Set up Cloudflare Web Analytics
- ✅ Monitor Core Web Vitals
- ✅ Configure uptime monitoring
- ✅ Set up error tracking

## Support

For deployment issues:
- **Cloudflare Pages Docs**: [developers.cloudflare.com/pages](https://developers.cloudflare.com/pages)
- **Next.js Static Export**: [nextjs.org/docs/app/building-your-application/deploying/static-exports](https://nextjs.org/docs/app/building-your-application/deploying/static-exports)
- **WaddleAI Issues**: [GitHub Issues](https://github.com/penguintechinc/waddleai/issues)

---

**Ready to deploy?** Push your changes to the main branch and Cloudflare Pages will automatically build and deploy your site to `waddlebot.ai`!
