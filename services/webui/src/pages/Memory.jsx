import { useState, useEffect } from 'react';
import axios from 'axios';
import './Memory.css';

const DEFAULT_MEMORY = { enabled: false, max_messages: 20, similarity_threshold: 0.7 };
const DEFAULT_RAG = { enabled: false, collection: 'default', top_k: 5, similarity_threshold: 0.7 };
const DEFAULT_EMBEDDING = {
  backend: 'ollama',
  model: 'nomic-embed-text',
  ollama_host: 'http://localhost:11434',
  dimensions: 768,
};

// Thin presentation layer over /api/v1/ailb/{memory-config,rag-config,
// embedding-config}. Ported from the legacy management plane's
// /memory-config admin page. The legacy page's ChromaDB-specific fields
// (chromadb_host/port) targeted a backend since dropped in favor of
// mem0-via-pgvector; this screen configures the current architecture's
// equivalent injection settings instead. All persistence/validation is
// server-side.
function Memory() {
  const [organizationId, setOrganizationId] = useState(1);
  const [memoryConfig, setMemoryConfig] = useState(DEFAULT_MEMORY);
  const [ragConfig, setRagConfig] = useState(DEFAULT_RAG);
  const [embeddingConfig, setEmbeddingConfig] = useState(DEFAULT_EMBEDDING);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  useEffect(() => {
    fetchAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [organizationId]);

  const fetchAll = async () => {
    try {
      setLoading(true);
      const [memoryRes, ragRes, embeddingRes] = await Promise.all([
        axios.get('/api/v1/ailb/memory-config', { params: { organization_id: organizationId } }),
        axios.get('/api/v1/ailb/rag-config', { params: { organization_id: organizationId } }),
        axios.get('/api/v1/ailb/embedding-config', { params: { organization_id: organizationId } }),
      ]);
      setMemoryConfig(memoryRes.data);
      setRagConfig(ragRes.data);
      setEmbeddingConfig(embeddingRes.data);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch memory configuration');
    } finally {
      setLoading(false);
    }
  };

  const saveSection = async (path, payload, label) => {
    try {
      await axios.post(path, { ...payload, organization_id: organizationId });
      setSuccess(`${label} saved successfully`);
      setError(null);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || `Failed to save ${label.toLowerCase()}`);
    }
  };

  if (loading) {
    return <div className="loading">Loading memory configuration...</div>;
  }

  return (
    <div className="memory">
      <div className="page-header">
        <h1>Memory &amp; Retrieval Configuration</h1>
        <div className="form-group org-select">
          <label htmlFor="organization-id">Organization ID</label>
          <input
            id="organization-id"
            type="number"
            min="1"
            value={organizationId}
            onChange={(e) => setOrganizationId(Number(e.target.value) || 1)}
          />
        </div>
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      {success && (
        <div className="alert alert-success">
          <strong>Success:</strong> {success}
          <button onClick={() => setSuccess(null)}>&times;</button>
        </div>
      )}

      <div className="memory-card">
        <h3>Conversation Memory (mem0 / pgvector)</h3>
        <div className="form-group">
          <label>
            <input
              type="checkbox"
              checked={!!memoryConfig.enabled}
              onChange={(e) => setMemoryConfig({ ...memoryConfig, enabled: e.target.checked })}
            />
            Enabled
          </label>
        </div>
        <div className="form-group">
          <label htmlFor="memory-max-messages">Max Messages</label>
          <input
            id="memory-max-messages"
            type="number"
            value={memoryConfig.max_messages}
            onChange={(e) => setMemoryConfig({ ...memoryConfig, max_messages: Number(e.target.value) })}
          />
        </div>
        <div className="form-group">
          <label htmlFor="memory-similarity">Similarity Threshold</label>
          <input
            id="memory-similarity"
            type="number"
            step="0.01"
            min="0"
            max="1"
            value={memoryConfig.similarity_threshold}
            onChange={(e) =>
              setMemoryConfig({ ...memoryConfig, similarity_threshold: Number(e.target.value) })
            }
          />
        </div>
        <button
          className="btn-primary"
          onClick={() => saveSection('/api/v1/ailb/memory-config', memoryConfig, 'Memory configuration')}
        >
          Save Memory Configuration
        </button>
      </div>

      <div className="memory-card">
        <h3>RAG Document Retrieval</h3>
        <div className="form-group">
          <label>
            <input
              type="checkbox"
              checked={!!ragConfig.enabled}
              onChange={(e) => setRagConfig({ ...ragConfig, enabled: e.target.checked })}
            />
            Enabled
          </label>
        </div>
        <div className="form-group">
          <label htmlFor="rag-collection">Collection</label>
          <input
            id="rag-collection"
            type="text"
            value={ragConfig.collection}
            onChange={(e) => setRagConfig({ ...ragConfig, collection: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label htmlFor="rag-top-k">Top K</label>
          <input
            id="rag-top-k"
            type="number"
            value={ragConfig.top_k}
            onChange={(e) => setRagConfig({ ...ragConfig, top_k: Number(e.target.value) })}
          />
        </div>
        <div className="form-group">
          <label htmlFor="rag-similarity">Similarity Threshold</label>
          <input
            id="rag-similarity"
            type="number"
            step="0.01"
            min="0"
            max="1"
            value={ragConfig.similarity_threshold}
            onChange={(e) => setRagConfig({ ...ragConfig, similarity_threshold: Number(e.target.value) })}
          />
        </div>
        <button
          className="btn-primary"
          onClick={() => saveSection('/api/v1/ailb/rag-config', ragConfig, 'RAG configuration')}
        >
          Save RAG Configuration
        </button>
      </div>

      <div className="memory-card">
        <h3>Embedding Backend</h3>
        <div className="form-group">
          <label htmlFor="embedding-backend">Backend</label>
          <select
            id="embedding-backend"
            value={embeddingConfig.backend}
            onChange={(e) => setEmbeddingConfig({ ...embeddingConfig, backend: e.target.value })}
          >
            <option value="ollama">Ollama</option>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </div>
        <div className="form-group">
          <label htmlFor="embedding-model">Model</label>
          <input
            id="embedding-model"
            type="text"
            value={embeddingConfig.model}
            onChange={(e) => setEmbeddingConfig({ ...embeddingConfig, model: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label htmlFor="embedding-host">Ollama Host</label>
          <input
            id="embedding-host"
            type="text"
            value={embeddingConfig.ollama_host}
            onChange={(e) => setEmbeddingConfig({ ...embeddingConfig, ollama_host: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label htmlFor="embedding-dimensions">Dimensions</label>
          <input
            id="embedding-dimensions"
            type="number"
            value={embeddingConfig.dimensions}
            onChange={(e) => setEmbeddingConfig({ ...embeddingConfig, dimensions: Number(e.target.value) })}
          />
        </div>
        <button
          className="btn-primary"
          onClick={() =>
            saveSection('/api/v1/ailb/embedding-config', embeddingConfig, 'Embedding configuration')
          }
        >
          Save Embedding Configuration
        </button>
      </div>
    </div>
  );
}

export default Memory;
