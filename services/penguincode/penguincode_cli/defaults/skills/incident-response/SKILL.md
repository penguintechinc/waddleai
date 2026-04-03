---
name: incident-response
description: "Production incident handling, diagnosis, and postmortem process"
model: qwen2.5-coder:7b
---

# Incident Response

## Overview
Handle production incidents systematically: detect, diagnose, mitigate, and learn.

## Incident Response Steps
1. **Detect** — alert fires or user reports issue
2. **Assess severity** — impact scope and urgency
3. **Communicate** — notify stakeholders
4. **Diagnose** — find root cause (see waddlepowers:monitoring-and-logging)
5. **Mitigate** — stop the bleeding (see waddlepowers:deployment-rollback)
6. **Fix** — implement proper solution
7. **Verify** — confirm issue is resolved
8. **Postmortem** — document and prevent recurrence

## Quick Diagnosis
```bash
# Check service health
curl -s http://app:8080/health | jq .

# Check logs
docker-compose logs --tail=100 <service>
kubectl logs -l app=<name> --tail=100

# Check resources (see waddlepowers:kubernetes-debugging)
kubectl top pods
docker stats
```

## Severity Levels
- **P1 Critical** — service down, all users affected
- **P2 Major** — significant functionality broken
- **P3 Minor** — limited impact, workaround available
- **P4 Low** — cosmetic or minor inconvenience

## Postmortem Template
1. **Summary** — what happened in 2-3 sentences
2. **Timeline** — chronological event log
3. **Root cause** — what caused the incident
4. **Impact** — users affected, duration, data loss
5. **Resolution** — what fixed it
6. **Action items** — preventive measures with owners
7. **Lessons learned** — what we'll do differently

## Rules
- Mitigate first, investigate second
- Don't blame individuals — focus on systems
- Write the postmortem within 48 hours
- Track action items to completion
