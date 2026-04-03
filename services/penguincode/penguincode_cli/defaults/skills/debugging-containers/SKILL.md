---
name: debugging-containers
description: "Container log inspection, exec, inspect, and network debugging"
model: qwen2.5-coder:7b
---

# Debugging Containers

## Overview
Diagnose issues with running containers using logs, exec, inspect, and network tools.

## Log Inspection
```bash
# View recent logs
docker logs <container> --tail 50

# Follow logs
docker logs <container> -f

# Logs with timestamps
docker logs <container> --timestamps

# Docker Compose logs
docker-compose logs -f <service>
```

## Interactive Debugging
```bash
# Shell into running container
docker exec -it <container> /bin/sh

# Run a command
docker exec <container> cat /etc/hosts

# Check processes
docker exec <container> ps aux
```

## Container Inspection
```bash
# Full container details
docker inspect <container>

# Check health status
docker inspect --format='{{.State.Health.Status}}' <container>

# View environment variables
docker inspect --format='{{json .Config.Env}}' <container> | jq .

# Check restart policy
docker inspect --format='{{.HostConfig.RestartPolicy}}' <container>
```

## Network Debugging
```bash
# List networks
docker network ls

# Inspect network
docker network inspect <network>

# Test connectivity from container
docker exec <container> curl -s http://other-service:8080/health

# DNS resolution
docker exec <container> nslookup other-service
```

## Common Issues
- **Container restarting**: check logs for crash reason
- **Connection refused**: verify service is listening on correct port/interface
- **Permission denied**: check file permissions and user
- **Out of memory**: check `docker stats` for resource usage
