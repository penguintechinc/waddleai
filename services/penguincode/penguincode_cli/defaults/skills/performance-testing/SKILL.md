---
name: performance-testing
description: "Load testing, benchmarking, and performance profiling"
model: qwen2.5-coder:7b
---

# Performance Testing

## Overview
Measure application performance under load to identify bottlenecks and ensure SLA compliance.

## Test Types
1. **Load testing** — normal expected traffic
2. **Stress testing** — beyond normal capacity
3. **Spike testing** — sudden traffic bursts
4. **Endurance testing** — sustained load over time

## Tools
- **wrk** — HTTP benchmarking: `wrk -t12 -c400 -d30s http://localhost:8080/api`
- **hey** — HTTP load generator: `hey -n 1000 -c 50 http://localhost:8080/api`
- **pytest-benchmark** — Python function benchmarking
- **pprof** — Go profiling

## Key Metrics
- **Latency**: p50, p95, p99 response times
- **Throughput**: requests per second
- **Error rate**: percentage of failed requests
- **Resource usage**: CPU, memory, connections

## Profiling
```bash
# Python
python -m cProfile -o profile.out app.py
# Go
go test -bench=. -cpuprofile=cpu.prof

# Memory
# Python: tracemalloc, memory_profiler
# Go: go tool pprof -alloc_space
```

## Best Practices
- Establish baseline metrics before optimizing
- Test with realistic data volumes
- Profile before optimizing — measure, don't guess
- Test on hardware similar to production
