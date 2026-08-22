# llama.cpp Integration

WaddleAI supports llama.cpp as a local inference provider, enabling GPU-accelerated inference on Kubernetes nodes without external API calls.

## Architecture

llama.cpp runs as a **K8s DaemonSet** — one llama.cpp server process per GPU node. WaddleAI's `LlamaCppManager` handles lifecycle: deploying, health-checking, and routing requests to the local server.

## Prerequisites

- Kubernetes cluster with GPU nodes
- llama.cpp server binary available in your container image
- Models stored on persistent volumes accessible to GPU nodes

## Configuration

### Environment Variables

```bash
LLAMACPP_HOST=localhost     # llama.cpp server host (default: localhost)
LLAMACPP_PORT=8080          # llama.cpp server port (default: 8080)
LLAMACPP_MODEL_PATH=/models # Path to GGUF model files
```

### Management API

Register a llama.cpp deployment:

```bash
curl -X POST http://localhost:8001/api/v1/llamacpp/deployments \
  -H "Authorization: Bearer $MGMT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "node_name": "gpu-node-01",
    "model_path": "/models/llama-3-8b.gguf",
    "context_length": 4096,
    "gpu_layers": 35
  }'
```

### Using llama.cpp Models

Route requests to llama.cpp by prefixing the model name:

```python
import openai

client = openai.OpenAI(
    api_key="<your-waddleai-key>",
    base_url="http://localhost:8000/v1"
)

response = client.chat.completions.create(
    model="llamacpp:llama-3-8b",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Helm Deployment

The llama.cpp DaemonSet is deployed alongside WaddleAI via the Helm chart:

```bash
helm upgrade --install waddleai ./k8s/helm/waddleai \
  --set llamacpp.enabled=true \
  --set llamacpp.modelPath=/models \
  --set llamacpp.gpuLayers=35
```

## Health Checks

The management server exposes llama.cpp health status:

```bash
curl http://localhost:8001/api/v1/llamacpp/health
```

## Air-Gapped Deployments

llama.cpp is ideal for air-gapped environments where external LLM API calls are not permitted. All inference runs locally on your GPU nodes — no data leaves the cluster.
