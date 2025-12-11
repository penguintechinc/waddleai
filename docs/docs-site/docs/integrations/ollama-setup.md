# Ollama Backend Setup

Use Ollama for fast, local LLM inference with WaddleAI.

## Why Ollama?

- **Free**: No API costs
- **Fast**: Local inference, low latency
- **Private**: Data never leaves your machine
- **Powerful**: Run models like Llama, CodeLlama, Mistral

## Installation

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### macOS

```bash
brew install ollama
```

### Windows

Download from [ollama.com](https://ollama.com/download)

### Docker

```bash
docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama
```

## Pull Models

### Routing Model (Required)

Fast model for routing decisions:

```bash
ollama pull llama3.2:1b
```

### General Purpose Models

```bash
# Fast, efficient
ollama pull llama3.2:3b

# Balanced performance
ollama pull llama3.2:8b

# Best quality
ollama pull llama3.2:70b
```

### Code Models

```bash
# Code generation
ollama pull codellama

# Code completion
ollama pull codellama:7b-code

# Best for code
ollama pull codellama:34b
```

### Specialized Models

```bash
# Fast chat
ollama pull mistral

# Analysis
ollama pull mixtral

# Embeddings
ollama pull nomic-embed-text
```

## Configure WaddleAI

### Add Ollama Provider

Management Portal:
1. Navigate to "LLM Providers"
2. Click "Add Provider"
3. Fill in:
   - **Name**: Local Ollama
   - **Type**: ollama
   - **Base URL**: http://localhost:11434
   - **API Key**: (leave empty)
   - **Enable**: ✓
4. Click "Test Connection"
5. Click "Save"

### Set as Routing LLM

Edit `.env`:

```bash
ROUTING_LLM_PROVIDER=ollama
ROUTING_LLM_MODEL=llama3.2:1b
ROUTING_LLM_ENDPOINT=http://localhost:11434
```

### Configure Routing

Set routing instructions to use Ollama:

Management Portal → Routing Configuration:

```
Route simple questions to llama3.2:3b.
Route programming to codellama.
Route complex analysis to mixtral.
Route everything else to llama3.2:3b.
```

## Docker Compose Integration

Add to `docker-compose.env.yml`:

```yaml
services:
  ollama:
    image: ollama/ollama
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  proxy:
    environment:
      - ROUTING_LLM_ENDPOINT=http://ollama:11434

volumes:
  ollama_data:
```

Pull models in container:

```bash
docker-compose exec ollama ollama pull llama3.2:1b
docker-compose exec ollama ollama pull codellama
```

## GPU Acceleration

### NVIDIA GPU

```bash
# Install NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker

# Run Ollama with GPU
docker run -d --gpus=all -v ollama:/root/.ollama -p 11434:11434 ollama/ollama
```

### AMD GPU

```bash
# Run with ROCm
docker run -d --device=/dev/kfd --device=/dev/dri -v ollama:/root/.ollama -p 11434:11434 ollama/ollama
```

### Apple Silicon

GPU automatically enabled on M1/M2/M3 Macs.

## Model Management

### List Models

```bash
ollama list
```

### Remove Model

```bash
ollama rm codellama
```

### Model Info

```bash
ollama show llama3.2:3b
```

### Custom Models

Create `Modelfile`:

```dockerfile
FROM llama3.2:3b

SYSTEM "You are a Python expert. Always provide clear, well-commented code."

PARAMETER temperature 0.7
PARAMETER top_k 40
PARAMETER top_p 0.9
```

Build and use:

```bash
ollama create python-expert -f Modelfile
ollama run python-expert "Write a Python function"
```

## Performance Tuning

### System Resources

**CPU Only**:
```bash
# Use smaller models
ollama pull llama3.2:1b  # ~1GB RAM
ollama pull llama3.2:3b  # ~2GB RAM
```

**GPU**:
```bash
# Use larger models
ollama pull llama3.2:70b  # ~40GB VRAM
ollama pull codellama:34b # ~20GB VRAM
```

### Concurrent Requests

Set in Ollama:

```bash
OLLAMA_NUM_PARALLEL=4 ollama serve
```

### Context Length

```bash
# Longer context
ollama run llama3.2:3b --num-ctx 8192
```

## Testing

### Direct Test

```bash
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.2:3b",
  "prompt": "Why is the sky blue?",
  "stream": false
}'
```

### Via WaddleAI

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wai_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2:3b",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Monitoring

### Ollama Logs

```bash
# macOS
tail -f ~/.ollama/logs/server.log

# Linux
journalctl -u ollama -f

# Docker
docker logs -f ollama
```

### Resource Usage

```bash
# Monitor GPU
nvidia-smi -l 1

# Monitor CPU/RAM
htop
```

### WaddleAI Analytics

Management Portal → Analytics:
- Filter by provider: ollama
- View requests, tokens, response times
- Compare with cloud providers

## Troubleshooting

### "Connection refused"

```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# Restart Ollama
# macOS
brew services restart ollama

# Linux
sudo systemctl restart ollama

# Docker
docker restart ollama
```

### "Model not found"

```bash
# List available models
ollama list

# Pull missing model
ollama pull llama3.2:3b
```

### Slow Performance

1. Use GPU if available
2. Use smaller models for simple tasks
3. Increase OLLAMA_NUM_PARALLEL
4. Check system resources

### Out of Memory

```bash
# Use quantized models
ollama pull llama3.2:3b-q4  # 4-bit quantization

# Or smaller models
ollama pull llama3.2:1b
```

## Cost Comparison

### Ollama (Local)

- **Cost**: $0 (free)
- **Latency**: 50-200ms
- **Quality**: Good for most tasks
- **Hardware**: Requires GPU for best performance

### OpenAI GPT-3.5

- **Cost**: $0.0015 per 1K tokens
- **Latency**: 500-2000ms
- **Quality**: Excellent
- **Hardware**: None required

### Hybrid Strategy

Use WaddleAI routing:
- Simple queries → Ollama (free, fast)
- Complex queries → GPT-4 (best quality)
- Code → CodeLlama (Ollama, optimized)

**Estimated savings**: 70-90% compared to all-cloud

## Recommended Models

### By Use Case

| Use Case | Model | Size | Quality |
|----------|-------|------|---------|
| Routing | llama3.2:1b | 1GB | Fast |
| Chat | llama3.2:3b | 2GB | Good |
| Code | codellama | 4GB | Excellent |
| Analysis | mixtral | 26GB | Excellent |
| Embeddings | nomic-embed-text | 274MB | Good |

### By Hardware

**4GB RAM, No GPU**:
- llama3.2:1b
- llama3.2:3b (slow)

**8GB RAM, No GPU**:
- llama3.2:3b
- codellama:7b
- mistral

**16GB RAM + GPU**:
- llama3.2:8b
- codellama:13b
- mixtral

**32GB+ RAM + GPU**:
- llama3.2:70b
- codellama:34b
- All models

## Next Steps

- [Configure Routing](../getting-started/configuration.md)
- [Anthropic Setup](anthropic-config.md)
- [OpenAI Setup](openai-config.md)