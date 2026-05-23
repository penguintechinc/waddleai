# WaddleAI App-Specific Standards

This file supplements the root documentation with app-specific architecture, requirements, and context unique to WaddleAI.

## llama.cpp Provider

WaddleAI supports direct llama-server (llama.cpp) connections for lower-latency, higher-throughput inference compared to Ollama.

### Deployment model

Each `llamacpp_deployment` maps 1:1 to a K8s DaemonSet. One DaemonSet runs on each matching GPU node,
serving a single GGUF model. Multiple deployments = multiple models on different node pools.

### K8s node labelling convention

Label GPU nodes before creating deployments:

```bash
# Target A100 nodes
kubectl label node <node-name> waddleai/gpu-tier=a100

# Target H100 nodes
kubectl label node <node-name> waddleai/gpu-tier=h100

# Target any GPU node
kubectl label node <node-name> waddleai/gpu=true
```

Set `node_selector` in the deployment config to match:
```json
{"waddleai/gpu-tier": "a100"}
```

### GGUF sourcing

`model_url` should point to a publicly accessible GGUF file (HuggingFace raw URL, S3 pre-signed URL, etc.).
The initContainer downloads it into an `emptyDir` volume on first pod start.
**Note:** `emptyDir` is ephemeral — the model re-downloads on pod restart. Use a PersistentVolume for
production deployments with large models.

### Remote connect

Set `deployment_type=remote` and provide `endpoint_url` pointing at an existing llama-server.
WaddleAI performs a `/health` check before registering.
