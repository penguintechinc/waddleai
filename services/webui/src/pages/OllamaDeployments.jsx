import { useState, useEffect } from 'react';
import axios from 'axios';
import './OllamaDeployments.css';

function OllamaDeployments() {
  const [deployments, setDeployments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newDeployment, setNewDeployment] = useState({
    name: '',
    endpoint_url: '',
    deployment_type: 'docker',
    gpu_config: { gpu_count: 1 },
    auto_start: true
  });

  useEffect(() => {
    fetchDeployments();
  }, []);

  const fetchDeployments = async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/ollama/deployments');
      setDeployments(response.data.deployments || []);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch deployments');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateDeployment = async (e) => {
    e.preventDefault();
    try {
      await axios.post('/api/v1/ollama/deployments', newDeployment);
      setShowCreateForm(false);
      setNewDeployment({
        name: '',
        endpoint_url: '',
        deployment_type: 'docker',
        gpu_config: { gpu_count: 1 },
        auto_start: true
      });
      fetchDeployments();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create deployment');
    }
  };

  const handleDeleteDeployment = async (id) => {
    if (!window.confirm('Are you sure you want to delete this deployment?')) {
      return;
    }
    try {
      await axios.delete(`/api/v1/ollama/deployments/${id}`);
      fetchDeployments();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to delete deployment');
    }
  };

  const handlePullModel = async (deploymentId, modelName) => {
    try {
      await axios.post(`/api/v1/ollama/deployments/${deploymentId}/models/pull`, {
        model: modelName
      });
      alert(`Started pulling ${modelName}`);
      fetchDeployments();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to pull model');
    }
  };

  if (loading) {
    return <div className="loading">Loading deployments...</div>;
  }

  return (
    <div className="ollama-deployments">
      <div className="page-header">
        <h1>Ollama Deployments</h1>
        <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
          + New Deployment
        </button>
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      {showCreateForm && (
        <div className="modal">
          <div className="modal-content">
            <h2>Create Ollama Deployment</h2>
            <form onSubmit={handleCreateDeployment}>
              <div className="form-group">
                <label>Deployment Name</label>
                <input
                  type="text"
                  value={newDeployment.name}
                  onChange={(e) => setNewDeployment({ ...newDeployment, name: e.target.value })}
                  placeholder="ollama-node-1"
                  required
                />
              </div>

              <div className="form-group">
                <label>Endpoint URL</label>
                <input
                  type="url"
                  value={newDeployment.endpoint_url}
                  onChange={(e) => setNewDeployment({ ...newDeployment, endpoint_url: e.target.value })}
                  placeholder="http://ollama-node-1:11434"
                  required
                />
              </div>

              <div className="form-group">
                <label>Deployment Type</label>
                <select
                  value={newDeployment.deployment_type}
                  onChange={(e) => setNewDeployment({ ...newDeployment, deployment_type: e.target.value })}
                >
                  <option value="docker">Docker</option>
                  <option value="kubernetes">Kubernetes</option>
                  <option value="external">External</option>
                </select>
              </div>

              <div className="form-group">
                <label>GPU Count</label>
                <input
                  type="number"
                  min="0"
                  max="8"
                  value={newDeployment.gpu_config.gpu_count}
                  onChange={(e) => setNewDeployment({
                    ...newDeployment,
                    gpu_config: { gpu_count: parseInt(e.target.value) }
                  })}
                />
              </div>

              <div className="form-group checkbox">
                <label>
                  <input
                    type="checkbox"
                    checked={newDeployment.auto_start}
                    onChange={(e) => setNewDeployment({ ...newDeployment, auto_start: e.target.checked })}
                  />
                  Auto-start deployment
                </label>
              </div>

              <div className="form-actions">
                <button type="submit" className="btn-primary">Create</button>
                <button type="button" className="btn-secondary" onClick={() => setShowCreateForm(false)}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="deployments-grid">
        {deployments.length === 0 ? (
          <div className="empty-state">
            <p>No Ollama deployments configured</p>
            <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
              Create First Deployment
            </button>
          </div>
        ) : (
          deployments.map((deployment) => (
            <div key={deployment.id} className="deployment-card">
              <div className="deployment-header">
                <h3>{deployment.name}</h3>
                <span className={`status-badge ${deployment.status}`}>
                  {deployment.status}
                </span>
              </div>

              <div className="deployment-details">
                <div className="detail-row">
                  <span className="label">Endpoint:</span>
                  <span className="value">{deployment.endpoint_url}</span>
                </div>
                <div className="detail-row">
                  <span className="label">Type:</span>
                  <span className="value">{deployment.deployment_type}</span>
                </div>
                <div className="detail-row">
                  <span className="label">GPUs:</span>
                  <span className="value">{deployment.gpu_config?.gpu_count || 0}</span>
                </div>
                <div className="detail-row">
                  <span className="label">Health:</span>
                  <span className={`value ${deployment.health_status}`}>
                    {deployment.health_status || 'unknown'}
                  </span>
                </div>
              </div>

              <div className="deployment-models">
                <h4>Models</h4>
                {deployment.models && deployment.models.length > 0 ? (
                  <ul className="model-list">
                    {deployment.models.map((model) => (
                      <li key={model.id}>
                        {model.model_name}:{model.model_tag || 'latest'}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="no-models">No models pulled</p>
                )}
              </div>

              <div className="deployment-actions">
                <button
                  className="btn-small"
                  onClick={() => {
                    const modelName = prompt('Enter model name to pull (e.g., llama3.2):');
                    if (modelName) {
                      handlePullModel(deployment.id, modelName);
                    }
                  }}
                >
                  Pull Model
                </button>
                <button
                  className="btn-small btn-danger"
                  onClick={() => handleDeleteDeployment(deployment.id)}
                >
                  Delete
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default OllamaDeployments;
