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
function errMsg(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function OllamaDeployments() {
  const fleetEnabled = useVllmRayEnabled();
  const [pricing, setPricing] = useState(NO_PRICING);
  const [deployments, setDeployments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingDeployment, setEditingDeployment] = useState(null);
  const [editForm, setEditForm] = useState({ name: '', endpoint_url: '', deployment_type: 'docker', gpu_count: 1 });
  const [exportModal, setExportModal] = useState(null);
  const [logsModal, setLogsModal] = useState(null);
  const [assignForm, setAssignForm] = useState({ deployment_id: '', model_name: '', model_tag: 'latest' });
  const [bulkModels, setBulkModels] = useState('');
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
      setError(errMsg(err, 'Failed to fetch deployments'));
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
      setError(errMsg(err, 'Failed to create deployment'));
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
      setError(errMsg(err, 'Failed to delete deployment'));
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
      setError(errMsg(err, 'Failed to pull model'));
    }
  };

  const openEditModal = (d) => {
    setEditingDeployment(d);
    setEditForm({
      name: d.name || '',
      endpoint_url: d.endpoint_url || '',
      deployment_type: d.deployment_type || 'docker',
      gpu_count: d.gpu_config?.gpu_count ?? 1,
    });
  };

  const handleUpdateDeployment = async (e) => {
    e.preventDefault();
    if (!editingDeployment) return;
    try {
      await axios.put(`/api/v1/ollama/deployments/${editingDeployment.id}`, {
        name: editForm.name,
        endpoint_url: editForm.endpoint_url,
        deployment_type: editForm.deployment_type,
        gpu_config: { gpu_count: Number(editForm.gpu_count) || 0 },
      });
      setEditingDeployment(null);
      setSuccess('Deployment updated successfully');
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, 'Failed to update deployment'));
    }
  };

  const handleLifecycle = async (id, action) => {
    try {
      await axios.post(`/api/v1/ollama/deployments/${id}/${action}`);
      setSuccess(`Deployment ${action} triggered`);
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, `Failed to ${action} deployment`));
    }
  };

  const handleCheckHealth = async (id) => {
    try {
      const res = await axios.get(`/api/v1/ollama/deployments/${id}/health`);
      setSuccess(`Health: ${res.data?.status || res.data?.health_status || 'ok'}`);
    } catch (err) {
      setError(errMsg(err, 'Failed to check health'));
    }
  };

  const handleViewLogs = async (id) => {
    try {
      const res = await axios.get(`/api/v1/ollama/deployments/${id}/logs`);
      setLogsModal({ id, logs: res.data?.logs || JSON.stringify(res.data, null, 2) });
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch logs'));
    }
  };

  const handleRemoveModel = async (deploymentId, modelName) => {
    try {
      await axios.delete(`/api/v1/ollama/deployments/${deploymentId}/models/${encodeURIComponent(modelName)}`);
      setSuccess(`Removed ${modelName}`);
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, 'Failed to remove model'));
    }
  };

  const handleExport = async (url, title) => {
    try {
      const res = await axios.get(url);
      const content = typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2);
      setExportModal({ title, content });
    } catch (err) {
      setError(errMsg(err, `Failed to export ${title}`));
    }
  };

  const handleSyncDeploymentModels = async (deploymentId) => {
    try {
      await axios.post(`/api/v1/ollama/deployments/${deploymentId}/sync-models`);
      setSuccess('Models synced');
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, 'Failed to sync models'));
    }
  };

  const handleModelAction = async (modelId, action, body = undefined) => {
    try {
      const url = `/api/v1/ollama/models/${modelId}/${action}`;
      const res = action === 'route-status' ? await axios.get(url) : await axios.post(url, body);
      setSuccess(`${action}: ${JSON.stringify(res.data?.status || res.data)}`);
      if (action !== 'route-status') fetchDeployments();
    } catch (err) {
      setError(errMsg(err, `Failed to ${action} model`));
    }
  };

  const handleAssignModel = async (e) => {
    e.preventDefault();
    try {
      await axios.post('/api/v1/ollama/models/assign', {
        deployment_id: Number(assignForm.deployment_id),
        model_name: assignForm.model_name,
        model_tag: assignForm.model_tag,
      });
      setSuccess('Model assigned');
      setAssignForm({ deployment_id: '', model_name: '', model_tag: 'latest' });
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, 'Failed to assign model'));
    }
  };

  const handleBulkAssign = async () => {
    try {
      const models = bulkModels
        .split(',')
        .map((m) => m.trim())
        .filter(Boolean);
      await axios.post('/api/v1/ollama/models/bulk-assign', {
        deployment_id: Number(assignForm.deployment_id),
        models,
      });
      setSuccess('Bulk assignment completed');
      setBulkModels('');
      fetchDeployments();
    } catch (err) {
      setError(errMsg(err, 'Failed to bulk assign models'));
    }
  };

  if (loading) {
    return <div className="loading">Loading deployments...</div>;
  }

  return (
    <div className="ollama-deployments">
      <div className="page-header">
        <h1>Ollama Deployments</h1>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            className="btn-secondary"
            onClick={() => handleExport('/api/v1/ollama/export/metallb-all', 'All MetalLB Services')}
          >
            Export All MetalLB
          </button>
          <button className="btn-primary" onClick={() => setShowCreateForm(true)}>
            + New Deployment
          </button>
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
          {success}
          <button onClick={() => setSuccess(null)}>&times;</button>
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
                        <span>
                          {model.model_name}:{model.model_tag || 'latest'}
                        </span>
                        <button
                          className="btn-small"
                          onClick={() => handleModelAction(model.id, 'route-status')}
                        >
                          Route Status
                        </button>
                        <button
                          className="btn-small"
                          onClick={() => handleModelAction(model.id, 'sync')}
                        >
                          Sync
                        </button>
                        <button
                          className="btn-small"
                          onClick={() => handleModelAction(model.id, 'reassign', { deployment_id: deployment.id })}
                        >
                          Reassign
                        </button>
                        <button
                          className="btn-small btn-danger"
                          onClick={() => handleRemoveModel(deployment.id, model.model_name)}
                        >
                          Remove
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="no-models">No models pulled</p>
                )}
              </div>

              <div className="deployment-actions">
                <button className="btn-small" onClick={() => openEditModal(deployment)}>
                  Edit
                </button>
                <button className="btn-small" onClick={() => handleLifecycle(deployment.id, 'start')}>
                  Start
                </button>
                <button className="btn-small" onClick={() => handleLifecycle(deployment.id, 'stop')}>
                  Stop
                </button>
                <button className="btn-small" onClick={() => handleLifecycle(deployment.id, 'restart')}>
                  Restart
                </button>
                <button className="btn-small" onClick={() => handleCheckHealth(deployment.id)}>
                  Health
                </button>
                <button className="btn-small" onClick={() => handleViewLogs(deployment.id)}>
                  Logs
                </button>
                <button className="btn-small" onClick={() => handleSyncDeploymentModels(deployment.id)}>
                  Sync Models
                </button>
                <button
                  className="btn-small"
                  onClick={() =>
                    handleExport(
                      `/api/v1/ollama/deployments/${deployment.id}/k8s-manifest`,
                      'K8s Manifest'
                    )
                  }
                >
                  K8s Manifest
                </button>
                <button
                  className="btn-small"
                  onClick={() =>
                    handleExport(
                      `/api/v1/ollama/deployments/${deployment.id}/metallb-service`,
                      'MetalLB Service'
                    )
                  }
                >
                  MetalLB Service
                </button>
                <button
                  className="btn-small"
                  onClick={() =>
                    handleExport(
                      `/api/v1/ollama/deployments/${deployment.id}/metallb-model-services`,
                      'MetalLB Model Services'
                    )
                  }
                >
                  MetalLB Models
                </button>
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

      <section className="ollama-assign-section" style={{ marginTop: '1.5rem' }}>
        <h3>Model Assignment</h3>
        <form onSubmit={handleAssignModel} style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
          <input
            type="number"
            aria-label="Assign Deployment ID"
            placeholder="Deployment ID"
            value={assignForm.deployment_id}
            onChange={(e) => setAssignForm({ ...assignForm, deployment_id: e.target.value })}
            required
          />
          <input
            type="text"
            aria-label="Assign Model Name"
            placeholder="Model name"
            value={assignForm.model_name}
            onChange={(e) => setAssignForm({ ...assignForm, model_name: e.target.value })}
            required
          />
          <button type="submit" className="btn-primary">
            Assign Model
          </button>
          <input
            type="text"
            aria-label="Bulk Models"
            placeholder="Comma-separated models"
            value={bulkModels}
            onChange={(e) => setBulkModels(e.target.value)}
          />
          <button type="button" className="btn-secondary" onClick={handleBulkAssign}>
            Bulk Assign
          </button>
        </form>
      </section>

      {editingDeployment && (
        <div className="modal">
          <div className="modal-content">
            <h2>Edit Ollama Deployment</h2>
            <form onSubmit={handleUpdateDeployment}>
              <div className="form-group">
                <label htmlFor="edit-ollama-name">Name</label>
                <input
                  id="edit-ollama-name"
                  type="text"
                  value={editForm.name}
                  onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                  required
                />
              </div>
              <div className="form-group">
                <label htmlFor="edit-ollama-endpoint">Endpoint URL</label>
                <input
                  id="edit-ollama-endpoint"
                  type="url"
                  value={editForm.endpoint_url}
                  onChange={(e) => setEditForm({ ...editForm, endpoint_url: e.target.value })}
                  required
                />
              </div>
              <div className="form-actions">
                <button type="submit" className="btn-primary">Save Deployment</button>
                <button type="button" className="btn-secondary" onClick={() => setEditingDeployment(null)}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {exportModal && (
        <div className="modal">
          <div className="modal-content">
            <h2>{exportModal.title}</h2>
            <pre>{exportModal.content}</pre>
            <div className="form-actions">
              <button type="button" className="btn-secondary" onClick={() => setExportModal(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {logsModal && (
        <div className="modal">
          <div className="modal-content">
            <h2>Deployment #{logsModal.id} Logs</h2>
            <pre>{logsModal.logs}</pre>
            <div className="form-actions">
              <button type="button" className="btn-secondary" onClick={() => setLogsModal(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default OllamaDeployments;
