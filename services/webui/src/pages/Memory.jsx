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
const DEFAULT_SCOPING = { enabled: true, relevance_cutoff: 0.7, top_k: 3, configured: false };

// Thin presentation layer over /api/v1/{memory-config,rag-config,
// embedding-config,memory-scoping} and /api/v1/memory/<id>/{promote,correct,dispute}.
function Memory() {
  const [organizationId, setOrganizationId] = useState(1);
  const [memoryConfig, setMemoryConfig] = useState(DEFAULT_MEMORY);
  const [ragConfig, setRagConfig] = useState(DEFAULT_RAG);
  const [embeddingConfig, setEmbeddingConfig] = useState(DEFAULT_EMBEDDING);
  const [scopingConfig, setScopingConfig] = useState(DEFAULT_SCOPING);
  const [itemId, setItemId] = useState('');
  const [promoteScope, setPromoteScope] = useState('repo');
  const [scopeRef, setScopeRef] = useState('');
  const [correctionContent, setCorrectionContent] = useState('');
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
      const [memoryRes, ragRes, embeddingRes, scopingRes] = await Promise.all([
        axios.get('/api/v1/memory-config', { params: { organization_id: organizationId } }),
        axios.get('/api/v1/rag-config', { params: { organization_id: organizationId } }),
        axios.get('/api/v1/embedding-config', { params: { organization_id: organizationId } }),
        axios.get('/api/v1/memory-scoping', { params: { organization_id: organizationId } }),
      ]);
      setMemoryConfig(memoryRes.data);
      setRagConfig(ragRes.data);
      setEmbeddingConfig(embeddingRes.data);
      setScopingConfig(scopingRes.data);
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

  const handlePromote = async () => {
    const id = Number(itemId);
    if (!id) {
      setError('Memory Item ID is required');
      return;
    }
    try {
      const payload = { target_scope: promoteScope };
      if (promoteScope !== 'org') {
        payload.scope_ref = scopeRef;
      }
      const res = await axios.post(`/api/v1/memory/${id}/promote`, payload);
      setSuccess(`Memory #${id} promoted to ${res.data?.scope_type || promoteScope}`);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to promote memory item');
    }
  };

  const handleCorrect = async () => {
    const id = Number(itemId);
    if (!id) {
      setError('Memory Item ID is required');
      return;
    }
    if (!correctionContent.trim()) {
      setError('Correction content is required');
      return;
    }
    try {
      const res = await axios.post(`/api/v1/memory/${id}/correct`, { content: correctionContent });
      setSuccess(`Memory #${id} ${res.data?.status || 'corrected'} (v${res.data?.version ?? ''})`);
      setCorrectionContent('');
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to correct memory item');
    }
  };

  const handleDispute = async () => {
    const id = Number(itemId);
    if (!id) {
      setError('Memory Item ID is required');
      return;
    }
    try {
      await axios.post(`/api/v1/memory/${id}/dispute`, {});
      setSuccess(`Memory #${id} flagged as disputed`);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to dispute memory item');
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
          onClick={() => saveSection('/api/v1/memory-config', memoryConfig, 'Memory configuration')}
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
          onClick={() => saveSection('/api/v1/rag-config', ragConfig, 'RAG configuration')}
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
            saveSection('/api/v1/embedding-config', embeddingConfig, 'Embedding configuration')
          }
        >
          Save Embedding Configuration
        </button>
      </div>

      <div className="memory-card">
        <h3>Memory Scoping (§9.4)</h3>
        <div className="form-group">
          <label>
            <input
              type="checkbox"
              checked={!!scopingConfig.enabled}
              onChange={(e) => setScopingConfig({ ...scopingConfig, enabled: e.target.checked })}
            />
            Scoping Enabled
          </label>
        </div>
        <div className="form-group">
          <label htmlFor="scoping-relevance-cutoff">Relevance Cutoff</label>
          <input
            id="scoping-relevance-cutoff"
            type="number"
            step="0.01"
            min="0"
            max="1"
            value={scopingConfig.relevance_cutoff}
            onChange={(e) =>
              setScopingConfig({ ...scopingConfig, relevance_cutoff: Number(e.target.value) })
            }
          />
        </div>
        <div className="form-group">
          <span>Top-K Injection: {scopingConfig.top_k ?? 3}</span>
        </div>
        <button
          className="btn-primary"
          onClick={() =>
            saveSection(
              '/api/v1/memory-scoping',
              {
                enabled: scopingConfig.enabled,
                relevance_cutoff: scopingConfig.relevance_cutoff,
              },
              'Memory scoping'
            )
          }
        >
          Save Scoping Configuration
        </button>
      </div>

      <div className="memory-card">
        <h3>Memory Item Actions (§9.7)</h3>
        <div className="form-group">
          <label htmlFor="memory-item-id">Memory Item ID</label>
          <input
            id="memory-item-id"
            type="number"
            min="1"
            value={itemId}
            onChange={(e) => setItemId(e.target.value)}
            placeholder="Enter memory item ID"
          />
        </div>
        <div className="form-group">
          <label htmlFor="promote-target-scope">Promote Target Scope</label>
          <select
            id="promote-target-scope"
            value={promoteScope}
            onChange={(e) => setPromoteScope(e.target.value)}
          >
            <option value="repo">repo</option>
            <option value="project">project</option>
            <option value="org">org</option>
          </select>
        </div>
        {promoteScope !== 'org' && (
          <div className="form-group">
            <label htmlFor="promote-scope-ref">Scope Reference</label>
            <input
              id="promote-scope-ref"
              type="text"
              value={scopeRef}
              onChange={(e) => setScopeRef(e.target.value)}
              placeholder="e.g. org/repo or project-id"
            />
          </div>
        )}
        <div className="form-group">
          <label htmlFor="correction-content">Correction Content</label>
          <textarea
            id="correction-content"
            rows="2"
            value={correctionContent}
            onChange={(e) => setCorrectionContent(e.target.value)}
            placeholder="Corrected memory content..."
          />
        </div>
        <div className="memory-actions">
          <button className="btn-primary" onClick={handlePromote}>
            Promote Memory
          </button>
          <button className="btn-secondary" onClick={handleCorrect}>
            Correct Memory
          </button>
          <button className="btn-danger" onClick={handleDispute}>
            Dispute Memory
          </button>
        </div>
      </div>
    </div>
  );
}

export default Memory;
