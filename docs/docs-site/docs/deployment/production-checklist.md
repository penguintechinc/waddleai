# Production Deployment Checklist

Complete checklist for deploying WaddleAI to production safely and securely.

## Pre-Deployment

### Security

- [ ] **Change all default passwords**
  - Database password (`POSTGRES_PASSWORD`)
  - Redis password (`REDIS_PASSWORD`)
  - JWT secret (`JWT_SECRET`)
  - Admin password (`ADMIN_PASSWORD`)
  - Grafana password (`GRAFANA_PASSWORD`)
  - WebUI secret (`WEBUI_SECRET_KEY`)

- [ ] **Use strong secrets**
  - JWT secret: minimum 32 characters, random
  - Passwords: minimum 16 characters, complex
  - Generate with: `openssl rand -base64 32`

- [ ] **Configure TLS/SSL**
  - Valid SSL certificates (Let's Encrypt recommended)
  - HTTPS for management portal
  - WSS (WebSocket Secure) for MCP
  - TLS for database connections
  - TLS for Redis connections

- [ ] **Enable authentication**
  - API key authentication enabled
  - MCP authentication required (`MCP_AUTH_REQUIRED=true`)
  - Management portal login required
  - Disable anonymous access

- [ ] **Review firewall rules**
  - Only expose necessary ports
  - Restrict management portal access
  - Limit database/Redis access to internal network
  - Enable rate limiting at firewall level

- [ ] **Security scanning**
  - Run vulnerability scans on Docker images
  - Check dependencies for CVEs
  - Enable prompt injection detection
  - Configure content filtering

### Infrastructure

- [ ] **Database**
  - Use external PostgreSQL (not Docker volume for production)
  - Configure automated backups
  - Set up replication/failover
  - Tune connection pool size
  - Enable SSL connections

- [ ] **Redis**
  - Use external Redis (not Docker volume for production)
  - Configure persistence (RDB + AOF)
  - Set up Redis Sentinel or Cluster
  - Enable password authentication
  - Configure maxmemory policy

- [ ] **pgvector** (memory storage, part of the primary PostgreSQL database)
  - Use persistent storage
  - Configure backup strategy
  - Set appropriate retention policy
  - Monitor storage growth

- [ ] **Resource allocation**
  - Set appropriate CPU limits
  - Set appropriate memory limits
  - Configure swap if needed
  - Plan for horizontal scaling

### Configuration

- [ ] **Environment variables**
  - Use secret management (Vault, AWS Secrets Manager, etc.)
  - Never commit secrets to git
  - Use different .env files per environment
  - Validate all required variables are set

- [ ] **Logging**
  - Set `LOG_LEVEL=INFO` (not DEBUG)
  - Disable `LOG_REQUESTS` (sensitive data)
  - Configure log rotation
  - Set up centralized logging (ELK, Splunk, etc.)
  - Enable structured logging

- [ ] **Monitoring**
  - Prometheus scraping configured
  - Grafana dashboards set up
  - Alert rules defined
  - On-call rotation established
  - Health check endpoints monitored

- [ ] **Quotas and limits**
  - Set reasonable default quotas
  - Configure rate limiting
  - Set max concurrent requests
  - Configure request timeouts
  - Enable XDP rate limiting if available

## Deployment

### Docker Images

- [ ] **Build production images**
  ```bash
  docker build -t waddleai/proxy:v1.0.0 ./proxy
  docker build -t waddleai/management:v1.0.0 ./management
  ```

- [ ] **Scan images for vulnerabilities**
  ```bash
  docker scan waddleai/proxy:v1.0.0
  docker scan waddleai/management:v1.0.0
  ```

- [ ] **Push to registry**
  ```bash
  docker push waddleai/proxy:v1.0.0
  docker push waddleai/management:v1.0.0
  ```

### Database Setup

- [ ] **Create production database**
  ```sql
  CREATE DATABASE waddleai;
  CREATE USER waddleai WITH ENCRYPTED PASSWORD 'strong-password';
  GRANT ALL PRIVILEGES ON DATABASE waddleai TO waddleai;
  ```

- [ ] **Run migrations**
  ```bash
  python management/apps/management_server/migrations/run_migrations.py
  ```

- [ ] **Create admin user**
  ```bash
  python management/apps/management_server/create_admin.py \
    --username admin \
    --password "$(openssl rand -base64 16)" \
    --email admin@example.com
  ```

- [ ] **Verify database indexes**
  - Check query performance
  - Add indexes for common queries
  - Run VACUUM ANALYZE

### Service Deployment

- [ ] **Deploy services**
  - Use orchestration (Kubernetes, Docker Swarm, etc.)
  - Configure health checks
  - Set restart policies
  - Configure resource limits

- [ ] **Verify connectivity**
  - Proxy ↔ Database (also serves pgvector memory storage)
  - Proxy ↔ Redis
  - Management ↔ All dependencies
  - External ↔ Proxy (port 8000)
  - External ↔ Management (port 8001)

- [ ] **Test endpoints**
  ```bash
  export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
  ```

  ```bash
  # Health checks
  curl https://api.waddleai.example.com/healthz
  curl https://manage.waddleai.example.com/healthz

  # API endpoints
  curl https://api.waddleai.example.com/v1/models \
    -H "Authorization: Bearer $WADDLEAI_API_KEY"

  # MCP endpoint
  wscat -c wss://api.waddleai.example.com:8765/mcp \
    -H "X-API-Key: $WADDLEAI_API_KEY"
  ```

### XDP/AF_XDP (Optional)

- [ ] **Kernel requirements**
  - Linux kernel 5.10+ with BPF support
  - `CONFIG_XDP_SOCKETS=y` in kernel config
  - libbpf installed

- [ ] **Compile BPF program**
  ```bash
  cd shared/networking
  ./compile_xdp.sh
  ```

- [ ] **Load XDP program**
  ```bash
  # Find network interface
  ip link show

  # Load XDP program
  xdp-loader load -m skb eth0 shared/networking/xdp_filter.o
  ```

- [ ] **Verify XDP is active**
  ```bash
  # Check XDP statistics
  curl https://manage.waddleai.example.com/api/performance/xdp \
    -H "Authorization: Bearer <your-admin-token>"
  ```

- [ ] **Configure rate limits**
  ```bash
  curl -X POST https://manage.waddleai.example.com/api/performance/xdp/rate-limits \
    -H "Authorization: Bearer <your-admin-token>" \
    -H "Content-Type: application/json" \
    -d '{
      "default_rate": 1000,
      "burst": 5000
    }'
  ```

## Post-Deployment

### Monitoring

- [ ] **Configure alerts**
  - High error rate
  - Quota exceeded
  - Database connection failures
  - Redis connection failures
  - High latency
  - Low disk space
  - High memory usage

- [ ] **Set up dashboards**
  - System metrics (CPU, memory, disk)
  - Request metrics (rate, latency, errors)
  - Token usage metrics
  - Model performance metrics
  - Routing decision metrics

- [ ] **Log aggregation**
  - Centralized logging configured
  - Log retention policy set
  - Log parsing rules defined
  - Alert rules for errors

### Performance

- [ ] **Baseline metrics**
  - Record baseline performance
  - Document expected latency
  - Measure throughput capacity
  - Identify bottlenecks

- [ ] **Load testing**
  ```bash
  # Use Apache Bench
  ab -n 10000 -c 100 \
    -H "Authorization: Bearer $WADDLEAI_API_KEY" \
    -p request.json \
    -T "application/json" \
    https://api.waddleai.example.com/v1/chat/completions

  # Or use Locust
  locust -f loadtest.py --host https://api.waddleai.example.com
  ```

- [ ] **Optimize settings**
  - Tune database connection pool
  - Tune Redis connection pool
  - Adjust max concurrent requests
  - Optimize request timeout
  - Enable connection keep-alive

### Security Audit

- [ ] **Access control**
  - Review user roles and permissions
  - Audit API key access
  - Check organization boundaries
  - Verify quota enforcement

- [ ] **Penetration testing**
  - Test prompt injection defenses
  - Test rate limiting
  - Test authentication bypass attempts
  - Test API abuse scenarios

- [ ] **Compliance**
  - GDPR compliance (data retention, export, deletion)
  - SOC 2 requirements if applicable
  - HIPAA requirements if applicable
  - Document security controls

### AI-Specific Security Audit (LLM Proxy)

> See [AI Security Recommendations](../administration/ai-security-recommendations.md) for full details.

- [ ] **Output guardrails deployed**
  - Output sanitization layer active on all LLM response paths
  - PII patterns blocked (email, credit card, API keys, passwords)
  - Output guardrail sits downstream of Redis cache (not just pre-LLM)
  - Verified: cached responses also pass through output filtering

- [ ] **Memory injection hardened against indirect prompt injection**
  - Past conversations injected via mem0/pgvector use structured delimiters
  - Injected context marked as data-only (not executable instructions)
  - System prompt explicitly instructs model to ignore instructions in retrieved context
  - pgvector queries filtered by `user_id` AND `organization_id` (no cross-tenant retrieval)

- [ ] **Semantic cache poisoning mitigated**
  - Cache keys use `Hash(TenantID + OrgID + UserRole)` namespace prefix
  - Similarity threshold set to ≥0.96 (not default 0.80–0.90)
  - Short TTL configured for cached LLM responses (≤30 minutes)
  - Anomaly alert: trigger if >20 distinct queries hit same cached entry within 1 hour

- [ ] **AI workload runtime hardening (Kubernetes)**
  - Pod Security Standards: `Restricted` profile enforced on AI workload namespaces
  - `securityContext.capabilities.drop: [ALL]` on all proxy/management pods
  - `readOnlyRootFilesystem: true` on LLM-adjacent containers
  - Kyverno policies block privileged AI pods from deploying
  - Cilium Tetragon TracingPolicy deployed: alert/kill on unexpected shell spawning

- [ ] **LLM provider credential security**
  - Provider API keys (OpenAI, Anthropic) not hardcoded in container env vars
  - Secrets managed via Vault, K8s Secrets with Sealed Secrets, or SPIFFE/SPIRE
  - Consider egress credential proxy (Infisical Agent Vault pattern) for AI workloads
  - API keys rotated regularly; old keys revoked

- [ ] **Model extraction rate limiting**
  - Anomaly detection for high-volume, diverse query patterns from single API key
  - Daily query caps per key prevent bulk model extraction attempts
  - Response fingerprinting to detect systematic probing

- [ ] **OWASP LLM Top 10 coverage verified**
  - [ ] LLM01: Prompt Injection — input guardrails active
  - [ ] LLM02: Insecure Output Handling — output guardrails active
  - [ ] LLM04: Model DoS — token bomb limits enforced
  - [ ] LLM06: Sensitive Information Disclosure — PII masking in output
  - [ ] LLM08: Excessive Agency — human-in-the-loop for high-risk actions
  - [ ] LLM09: Overreliance — monitoring and alerting for anomalous decisions

### Backup and Recovery

- [ ] **Automated backups**
  - Database backups (daily, covers pgvector memory tables)
  - Redis snapshots (hourly)
  - Configuration backups (on change)

- [ ] **Test restore procedures**
  - Restore database from backup (covers pgvector memory tables)
  - Restore Redis from snapshot
  - Verify data integrity

- [ ] **Disaster recovery plan**
  - Document recovery procedures
  - Define RTO (Recovery Time Objective)
  - Define RPO (Recovery Point Objective)
  - Test DR procedures quarterly

## Documentation

- [ ] **Runbooks**
  - Service restart procedures
  - Scaling procedures
  - Incident response procedures
  - Rollback procedures

- [ ] **Architecture documentation**
  - Network topology
  - Service dependencies
  - Data flow diagrams
  - Security architecture

- [ ] **Operational procedures**
  - User onboarding
  - API key generation
  - Quota management
  - Model configuration

## Ongoing Maintenance

### Daily

- [ ] Check error logs
- [ ] Monitor quota usage
- [ ] Review security alerts
- [ ] Check service health

### Weekly

- [ ] Review performance metrics
- [ ] Analyze token usage trends
- [ ] Check disk space growth
- [ ] Review routing decisions

### Monthly

- [ ] Update dependencies
- [ ] Review user access
- [ ] Audit API keys
- [ ] Optimize database
- [ ] Review and update documentation

### Quarterly

- [ ] Security audit
- [ ] Disaster recovery test
- [ ] Performance review
- [ ] Capacity planning
- [ ] Cost optimization review

## Rollback Plan

- [ ] **Document rollback steps**
  1. Stop new traffic to new version
  2. Switch load balancer to old version
  3. Verify old version is healthy
  4. Roll back database migrations if needed
  5. Monitor for issues

- [ ] **Keep previous version**
  - Maintain old Docker images
  - Keep old configuration
  - Document version compatibility

- [ ] **Test rollback**
  - Verify rollback works in staging
  - Time the rollback procedure
  - Document any issues

## Production Checklist Summary

✅ **Critical** (Must complete before launch):
- All passwords changed
- TLS/SSL configured
- Backups configured
- Monitoring set up
- Health checks working

⚠️ **Important** (Complete soon after launch):
- Load testing completed
- Alerts configured
- Documentation written
- DR plan tested

💡 **Recommended** (Improve over time):
- XDP acceleration enabled
- Advanced monitoring
- Automated scaling
- Cost optimization

## See Also

- [Docker Compose Deployment](docker-compose.md)
- [Kubernetes Deployment](kubernetes.md)
- [Security Policies](../administration/security-policies.md)
- [Monitoring Guide](../administration/monitoring.md)
