---
name: ssl-certificate-management
description: "SSL/TLS certificate creation, renewal, and Let's Encrypt setup"
model: qwen2.5-coder:7b
---

# SSL Certificate Management

## Overview
Manage SSL/TLS certificates for secure HTTPS connections, including Let's Encrypt automation.

## Let's Encrypt with Certbot
```bash
# Install certbot
apt-get install certbot

# Obtain certificate
certbot certonly --standalone -d example.com -d www.example.com

# Auto-renewal
certbot renew --dry-run

# Cron for renewal
echo "0 0 1 * * certbot renew --quiet" | crontab -
```

## Self-Signed (Development Only)
```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -sha256 -days 365 -nodes \
  -subj "/CN=localhost"
```

## Kubernetes TLS
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tls-secret
type: kubernetes.io/tls
data:
  tls.crt: <base64-encoded-cert>
  tls.key: <base64-encoded-key>
```

## cert-manager (Kubernetes)
```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: app-cert
spec:
  secretName: app-tls
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
    - app.example.com
```

## Certificate Monitoring
- Monitor certificate expiry dates
- Alert when certificates expire within 30 days
- Automate renewal wherever possible
- Never commit private keys to version control
