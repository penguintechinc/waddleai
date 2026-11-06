import { useState, useEffect } from 'react';
import axios from 'axios';
import { usePriceSuggestion, useVllmRayEnabled } from '../hooks/useVllmFleet';
import ChargebackStep from '../components/gpu/ChargebackStep';
import { validateStep } from '../components/gpu/wizard/wizardState';
import './OllamaDeployments.css';

const NO_PRICING = {
  priceMode: 1,
  pricesInput: '',
  pricesOutput: '',
  comparisonModel: '',
  gpuClass: '',
  paramsB: '',
};
const GPU_CLASS_LABEL = 'waddleai.penguintech.io/gpu-classes';

/**
 * Build the pricing fields of a create body. Empty when the pricing section was
 * left untouched, so existing creates stay byte-identical.
 * @param {object} pricing - the pricing section state
 * @param {{gpu_count:number}} gpuConfig
 * @returns {object}
 */
function pricingBody(pricing, gpuConfig) {
  if (!pricing.comparisonModel.trim()) return {};
  const body = {
    price_mode: pricing.priceMode,
    comparison_model: pricing.comparisonModel.trim(),
    gpu_config: {
      ...gpuConfig,
      node_selector: { [GPU_CLASS_LABEL]: pricing.gpuClass },
    },
  };
  if (pricing.priceMode === 2) {
    body.price_input_per_1m = pricing.pricesInput;
    body.price_output_per_1m = pricing.pricesOutput;
  }
  return body;
}

/**
 * Ollama deployments page. When the GPU Fleet flags are on, the create form
 * gains a chargeback section so the deployment gets a virtual price.
 * @returns {JSX.Element}
 */
function OllamaDeployments() {
  const fleetEnabled = useVllmRayEnabled();
  const [pricing, setPricing] = useState(NO_PRICING);
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

  const paramsB = Number(pricing.paramsB);
  const suggestion = usePriceSuggestion(
    {
      kind: 2,
      gpu_class: pricing.gpuClass,
      count: newDeployment.gpu_config.gpu_count,
      params_b: paramsB,
    },
    { enabled: fleetEnabled && showCreateForm && Boolean(pricing.gpuClass) && paramsB > 0 },
  );
  const priceErrors =
    fleetEnabled && pricing.comparisonModel.trim()
      ? validateStep(4, pricing).errors.concat(pricing.gpuClass.trim() ? [] : ['Enter the GPU class.'])
      : [];

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
      await axios.post('/api/v1/ollama/deployments', {
        ...newDeployment,
        ...(fleetEnabled ? pricingBody(pricing, newDeployment.gpu_config) : {}),
      });
      setPricing(NO_PRICING);
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

              {fleetEnabled && (
                <div className="pricing-section">
                  <label>
                    GPU class (for pricing)
                    <input
                      type="text"
                      value={pricing.gpuClass}
                      placeholder="nvidia-rtx4090"
                      onChange={(e) => setPricing({ ...pricing, gpuClass: e.target.value })}
                    />
                  </label>
                  <label>
                    Model size (billions of parameters, for pricing)
                    <input
                      type="number"
                      min="0"
                      step="any"
                      value={pricing.paramsB}
                      onChange={(e) => setPricing({ ...pricing, paramsB: e.target.value })}
                    />
                  </label>
                  <ChargebackStep
                    kind={2}
                    priceMode={pricing.priceMode}
                    pricesInput={pricing.pricesInput}
                    pricesOutput={pricing.pricesOutput}
                    comparisonModel={pricing.comparisonModel}
                    suggestion={suggestion.data}
                    suggestionError={suggestion.error?.message}
                    onChange={(patch) => setPricing({ ...pricing, ...patch })}
                  />
                  {priceErrors.length > 0 && (
                    <ul role="status">
                      {priceErrors.map((m) => (
                        <li key={m}>{m}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              <div className="form-actions">
                <button type="submit" className="btn-primary" disabled={priceErrors.length > 0}>Create</button>
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
