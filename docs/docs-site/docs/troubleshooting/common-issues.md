# Common Issues and Solutions

Quick solutions to common WaddleAI problems.

## Installation Issues

### "Python 3.13 not found"

**Problem**: System has older Python version

**Solution**:
```bash
# Ubuntu
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.13 python3.13-venv

# macOS
brew install python@3.13

# Verify
python3.13 --version
```

### "PostgreSQL connection failed"

**Problem**: Can't connect to database

**Solutions**:

1. **Check PostgreSQL is running**:
```bash
# Linux
sudo systemctl status postgresql
sudo systemctl start postgresql

# macOS
brew services start postgresql

# Docker
docker-compose ps postgres
```

2. **Verify connection string**:
```bash
# Test connection
psql postgresql://user:pass@host:5432/waddleai

# Check .env file
DATABASE_URL=postgresql://waddleai:password@localhost:5432/waddleai
```

3. **Create database**:
```bash
sudo -u postgres psql
CREATE DATABASE waddleai;
CREATE USER waddleai_user WITH PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE waddleai TO waddleai_user;
```

### "Redis connection refused"

**Problem**: Can't connect to Redis

**Solutions**:

1. **Start Redis**:
```bash
# Linux
sudo systemctl start redis
redis-cli ping  # Should return PONG

# macOS
brew services start redis

# Docker
docker-compose up -d redis
```

2. **Check Redis password**:
```bash
# Test with password
redis-cli -a your_password ping

# Update .env
REDIS_PASSWORD=your_password
```

## API Issues

### "Invalid API key"

**Problem**: API key not recognized

**Solutions**:

1. **Verify API key format**:
   - Must start with `wai_`
   - Check for extra spaces/newlines
   - Ensure full key was copied

2. **Check key is enabled**:
   - Management Portal → API Keys
   - Verify "Active" status
   - Check expiration date

3. **Verify Authorization header**:
```bash
# Correct format
Authorization: Bearer $WADDLEAI_API_KEY

# NOT
Authorization: $WADDLEAI_API_KEY
```

### "Quota exceeded"

**Problem**: Token limit reached

**Solutions**:

1. **Check current usage**:
   - Management Portal → API Keys → Your Key
   - View today's and monthly usage

2. **Increase limits**:
   - Edit API key
   - Increase daily/monthly limits
   - Or wait for daily reset (midnight UTC)

3. **View usage details**:
```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8001/api/analytics/usage?api_key=$WADDLEAI_API_KEY
```

### "Rate limit exceeded"

**Problem**: Too many requests

**Solutions**:

1. **Wait**: Rate limits reset every minute

2. **Increase limit**:
   - Management Portal → API Keys → Edit
   - Increase "Rate Limit (req/min)"

3. **Implement backoff**:
```python
import time

def request_with_retry(func, max_retries=3):
    for i in range(max_retries):
        try:
            return func()
        except RateLimitError:
            if i < max_retries - 1:
                time.sleep(2 ** i)  # Exponential backoff
            else:
                raise
```

### "Model not found"

**Problem**: Requested model not available

**Solutions**:

1. **Check available models**:
```bash
curl -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  http://localhost:8000/v1/models
```

2. **Add provider**:
   - Management Portal → LLM Providers → Add Provider
   - Configure OpenAI, Anthropic, or Ollama
   - Test connection

3. **Use auto-routing**:
```json
{
  "messages": [{"role": "user", "content": "Hello"}]
  // No model specified - WaddleAI chooses
}
```

## Routing Issues

### Slow routing decisions

**Problem**: Requests take long to route

**Solutions**:

1. **Check routing LLM**:
```bash
# Verify Ollama is running
curl http://localhost:11434/api/tags

# Pull fast model
ollama pull llama3.2:1b

# Update .env
ROUTING_LLM_MODEL=llama3.2:1b
```

2. **Enable routing cache**:
```bash
# .env
ROUTING_CACHE_TTL=300  # Cache for 5 minutes
```

3. **Check Redis connection**:
```bash
redis-cli -a $REDIS_PASSWORD ping
```

### Wrong model selected

**Problem**: Routing chooses unexpected model

**Solutions**:

1. **Review routing instructions**:
   - Management Portal → Routing Configuration
   - Update instructions
   - Test decision with "Test Decision" button

2. **Check provider priority**:
   - Management Portal → LLM Providers
   - Verify provider priorities
   - Ensure providers are enabled

3. **Use explicit model**:
```json
{
  "model": "gpt-4",  // Override routing
  "messages": [...]
}
```

4. **View routing logs**:
   - Management Portal → Analytics → Routing Decisions
   - Check why specific model was chosen

## Performance Issues

### High latency

**Problem**: Requests are slow

**Solutions**:

1. **Check provider response times**:
   - Management Portal → Analytics
   - View "Avg Response Time" by provider
   - Switch to faster providers if needed

2. **Enable XDP** (Linux only):
```bash
# .env
ENABLE_XDP=true

# Requires root/CAP_NET_ADMIN
sudo setcap cap_net_admin=eip $(which python3.13)
```

3. **Use local models for simple tasks**:
```bash
# Ollama for fast queries
ollama pull llama3.2:1b  # Very fast routing
ollama pull llama3.2:3b  # Fast general purpose
```

4. **Optimize database**:
```bash
# Increase connection pool
DATABASE_POOL_SIZE=50

# Add indexes
docker-compose exec proxy python -m shared.database.optimize
```

### High memory usage

**Problem**: Services consuming too much RAM

**Solutions**:

1. **Use smaller models**:
```bash
# Instead of llama3.2:70b (40GB)
ollama pull llama3.2:3b  # 2GB

# Or use quantized
ollama pull llama3.2:3b-q4  # Even smaller
```

2. **Limit Docker resources**:
```yaml
services:
  proxy:
    deploy:
      resources:
        limits:
          memory: 2G
```

3. **Restart services periodically**:
```bash
# Cron job to restart daily
0 3 * * * docker-compose -f /path/to/docker-compose.env.yml restart proxy
```

### Database slowdown

**Problem**: Database queries are slow

**Solutions**:

1. **Check connection pool**:
```bash
# .env
DATABASE_POOL_SIZE=50
DATABASE_POOL_TIMEOUT=30
```

2. **Optimize tables**:
```bash
docker-compose exec postgres psql -U waddleai -d waddleai
VACUUM ANALYZE;
REINDEX DATABASE waddleai;
```

3. **Archive old data**:
```bash
# Archive usage logs older than 90 days
docker-compose exec proxy python -m shared.database.archive_old_usage
```

## Memory Integration Issues

### ChromaDB connection failed

**Problem**: Can't connect to ChromaDB

**Solutions**:

1. **Check ChromaDB is running**:
```bash
# Docker
docker-compose ps chromadb

# Standalone
curl http://localhost:8000/api/v1/heartbeat
```

2. **Verify configuration**:
```bash
# .env
ENABLE_MEMORY=true
CHROMADB_HOST=localhost  # or chromadb in Docker
CHROMADB_PORT=8000
```

3. **Restart ChromaDB**:
```bash
docker-compose restart chromadb
```

### Conversation search not working

**Problem**: Can't find conversations

**Solutions**:

1. **Rebuild search index**:
   - Management Portal → Memory Configuration
   - Click "Rebuild Search Index"

2. **Enable semantic search**:
```bash
# .env
ENABLE_MEMORY=true
```

3. **Check ChromaDB storage**:
```bash
docker-compose exec chromadb ls -lh /chroma/chroma
```

## Security Issues

### Prompt injection detected

**Problem**: Requests being blocked

**Solutions**:

1. **Review security logs**:
   - Management Portal → Analytics → Security Events
   - Check what triggered detection

2. **Adjust sensitivity**:
```bash
# .env
ENABLE_PROMPT_INJECTION_DETECTION=true
PROMPT_INJECTION_ACTION=sanitize  # Instead of block
```

3. **Whitelist safe patterns**:
   - Management Portal → Security Configuration
   - Add allowed patterns

### JWT token expired

**Problem**: Authentication fails

**Solutions**:

1. **Re-login**:
   - Management Portal → Login
   - Enter credentials again

2. **Increase token lifetime**:
```bash
# .env
JWT_EXPIRY_HOURS=24  # Or longer
```

3. **Clear browser cookies**:
   - Browser settings → Clear cookies
   - Refresh page and login again

## Docker Issues

### Container won't start

**Problem**: Service fails to start

**Solutions**:

1. **Check logs**:
```bash
docker-compose logs service_name
```

2. **Check dependencies**:
```bash
# Ensure dependencies are healthy
docker-compose ps
```

3. **Recreate container**:
```bash
docker-compose up -d --force-recreate service_name
```

4. **Reset everything**:
```bash
docker-compose down -v
docker-compose up -d
```

### Port already in use

**Problem**: "Port 8000 is already allocated"

**Solutions**:

1. **Find process using port**:
```bash
lsof -i :8000
sudo kill -9 <PID>
```

2. **Change port in .env**:
```bash
PROXY_PORT=8080
```

3. **Update docker-compose.yml**:
```yaml
services:
  proxy:
    ports:
      - "8080:8000"
```

### Out of disk space

**Problem**: No space left on device

**Solutions**:

1. **Clean Docker**:
```bash
docker system prune -a
docker volume prune
```

2. **Check volumes**:
```bash
docker system df
```

3. **Archive logs**:
```bash
# Rotate logs
docker-compose exec proxy python -m shared.logging.rotate
```

## Networking Issues

### Can't access from other machines

**Problem**: WaddleAI only accessible from localhost

**Solutions**:

1. **Bind to all interfaces**:
```bash
# .env
PROXY_HOST=0.0.0.0
MANAGEMENT_HOST=0.0.0.0
```

2. **Check firewall**:
```bash
# Allow ports
sudo ufw allow 8000/tcp
sudo ufw allow 8001/tcp
```

3. **Docker network**:
```bash
# Check network
docker network inspect waddleai_waddleai
```

### SSL/TLS errors

**Problem**: HTTPS connection fails

**Solutions**:

1. **Use reverse proxy**:
   - Set up nginx with SSL
   - See [Production Guide](../deployment/production-checklist.md)

2. **Test without SSL**:
```bash
# Temporarily use HTTP
curl http://localhost:8000/healthz
```

3. **Check certificates**:
```bash
# Verify cert
openssl s_client -connect localhost:443
```

## Getting More Help

### Enable Debug Logging

```bash
# .env
LOG_LEVEL=DEBUG
LOG_FORMAT=text  # Easier to read

# View logs
docker-compose logs -f proxy
```

### Export Diagnostics

```bash
# Collect system info
docker-compose ps > diagnostics.txt
docker-compose logs >> diagnostics.txt
curl http://localhost:8000/healthz >> diagnostics.txt
```

### Report Issue

Include:
- WaddleAI version
- Error messages
- Docker logs
- Configuration (sanitize passwords!)

## Additional Resources

- [Configuration Reference](../getting-started/configuration.md)
- [API Documentation](../api/openai-compatible.md)
- [Deployment Guide](../deployment/docker-compose.md)
- [GitHub Issues](https://github.com/yourusername/WaddleAI/issues)
