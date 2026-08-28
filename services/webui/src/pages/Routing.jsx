import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { useAuth } from '../contexts/AuthContext';
import './Routing.css';

// Thin presentation layer over three routing-admin surfaces:
//   - /api/v1/routing/policies/<organization_id> (classifier_prompt / mode, org-scoped)
//   - /api/v1/routing/assignments/?tool_type=routing-classifier (the routing-classifier
//     model, global scope -- write-gated to admin server-side)
//   - /api/v1/routing/dry-run/ (admin-only, no-side-effect RoutingEngine.decide() preview)
// Ported from the legacy management plane's /routing-config admin page, then
// repointed from the retired /api/v1/routing-matrix/{instructions,test}
// endpoints (spec §7.6). The "Routing LLM Model" selector and "Test Routing
// Decision" tool were dropped when those legacy endpoints were retired
// (no real backing endpoint existed yet); both are restored here now that
// shared.routing.RoutingEngine + the routing-classifier model_assignments
// row + the dry-run endpoint are real. All persistence/validation is
// server-side.
function Routing() {
  const { user } = useAuth();
  const organizationId = user?.organization_id;
  const isAdmin = user?.role === 'admin';

  const [instructions, setInstructions] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // "Routing LLM Model" -- the routing-classifier tool_type's model_assignments
  // row (spec §7.2, §2.3). Fixed Gemma 4 option list rather than the model
  // registry: this assignment is pinned to the Gemma 4 family specifically
  // (shared/routing/classifier.py: "gemma4:e2b default, no dual-default
  // alternative required"), unlike ordinary tool-type assignments (chat,
  // code, ...) which can point at any capable model. There is also no live
  // model-registry endpoint yet -- migration 008 (model_registry) hasn't
  // landed (shared/routing/offers.py docstring) -- so even a registry-backed
  // dropdown isn't available to source options from today. Valid Gemma 4
  // tags only: e2b/e4b/12b/26b/31b -- there is no "2b" tag and "gemma4:2b"
  // is unpullable; Gemma 3 and PRC-origin models are never offered here.
  const GEMMA4_MODEL_OPTIONS = [
    { value: 'gemma4:e2b', label: 'Gemma 4 E2B (default)' },
    { value: 'gemma4:e4b', label: 'Gemma 4 E4B' },
    { value: 'gemma4:12b', label: 'Gemma 4 12B' },
    { value: 'gemma4:26b', label: 'Gemma 4 26B' },
    { value: 'gemma4:31b', label: 'Gemma 4 31B' },
  ];
  const [routingModel, setRoutingModel] = useState('gemma4:e2b');
  const [routingModelId, setRoutingModelId] = useState(null);
  const [routingModelLoading, setRoutingModelLoading] = useState(true);
  const [routingModelSaving, setRoutingModelSaving] = useState(false);

  // "Test Routing Decision" -- dry-run preview via RoutingEngine.decide(),
  // no side effects (no trace persisted, no upstream dispatch).
  const [testPrompt, setTestPrompt] = useState('');
  const [testToolType, setTestToolType] = useState('');
  const [testLoading, setTestLoading] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testError, setTestError] = useState(null);

  const fetchPolicy = useCallback(async () => {
    try {
      setLoading(true);
      const response = await axios.get(`/api/v1/routing/policies/${organizationId}`);
      const data = response.data.data || {};
      setInstructions(data.classifier_prompt || '');
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch routing policy');
    } finally {
      setLoading(false);
    }
  }, [organizationId]);

  // Shares the page-level error/success banner with fetchPolicy/handleSave
  // rather than a second alert box -- avoids two simultaneous "×"-dismiss
  // alerts fighting over the same accessible name when both mount-time
  // fetches fail together (matches the single-banner pattern this page
  // already used before these two controls were restored). Uses the
  // functional setError(prev => prev ?? ...) form so this fetch (which
  // starts second) never clobbers a policy-fetch error already surfacing.
  const fetchRoutingModel = useCallback(async () => {
    try {
      setRoutingModelLoading(true);
      const response = await axios.get('/api/v1/routing/assignments/', {
        params: { tool_type: 'routing-classifier' },
      });
      const entries = Array.isArray(response.data.data) ? response.data.data : [];
      const entry = entries.find((item) => item.scope === 'global') || entries[0] || null;
      if (entry) {
        setRoutingModel(entry.model_name || 'gemma4:e2b');
        setRoutingModelId(entry.id ?? null);
      }
    } catch (err) {
      const message = err.response?.data?.error || 'Failed to fetch routing LLM model';
      setError((prev) => prev ?? message);
    } finally {
      setRoutingModelLoading(false);
    }
  }, []);

  useEffect(() => {
    if (organizationId) {
      fetchPolicy();
      fetchRoutingModel();
    }
  }, [organizationId, fetchPolicy, fetchRoutingModel]);

  const handleSave = async (e) => {
    e.preventDefault();
    try {
      setSaving(true);
      await axios.put(`/api/v1/routing/policies/${organizationId}`, {
        classifier_prompt: instructions,
      });
      setSuccess('Routing configuration saved successfully');
      setError(null);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to save routing configuration');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveRoutingModel = async (e) => {
    e.preventDefault();
    try {
      setRoutingModelSaving(true);
      if (routingModelId) {
        await axios.put(`/api/v1/routing/assignments/${routingModelId}`, {
          model_name: routingModel,
        });
      } else {
        const response = await axios.post('/api/v1/routing/assignments/', {
          tool_type: 'routing-classifier',
          model_name: routingModel,
          scope: 'global',
        });
        setRoutingModelId(response.data.data?.id ?? null);
      }
      setError(null);
      setSuccess('Routing LLM model saved successfully');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to save routing LLM model');
    } finally {
      setRoutingModelSaving(false);
    }
  };

  const handleTestRouting = async (e) => {
    e.preventDefault();
    try {
      setTestLoading(true);
      setTestError(null);
      const payload = { prompt: testPrompt };
      if (testToolType.trim()) {
        payload.tool_type = testToolType.trim();
      }
      const response = await axios.post('/api/v1/routing/dry-run/', payload);
      setTestResult(response.data.data);
    } catch (err) {
      setTestError(err.response?.data?.error || 'Failed to run routing test');
      setTestResult(null);
    } finally {
      setTestLoading(false);
    }
  };

  if (!organizationId || loading) {
    return <div className="loading">Loading routing configuration...</div>;
  }

  return (
    <div className="routing">
      <div className="page-header">
        <h1>Routing Configuration</h1>
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

      <div className="routing-card">
        <h3>Routing LLM Model</h3>
        <p>
          The model used by the stage-2 routing classifier (spec §7.2) to determine tool type
          and complexity when explicit hints and heuristic rules don&apos;t resolve it.
        </p>
        {routingModelLoading ? (
          <p>Loading routing LLM model...</p>
        ) : (
          <form onSubmit={handleSaveRoutingModel}>
            <div className="form-group">
              <label htmlFor="routing-model">Routing LLM Model</label>
              {isAdmin ? (
                <select
                  id="routing-model"
                  value={routingModel}
                  onChange={(e) => setRoutingModel(e.target.value)}
                >
                  {GEMMA4_MODEL_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <p>{routingModel}</p>
              )}
            </div>
            {isAdmin && (
              <div className="form-actions">
                <button type="submit" className="btn-primary" disabled={routingModelSaving}>
                  {routingModelSaving ? 'Saving...' : 'Save Routing LLM Model'}
                </button>
              </div>
            )}
          </form>
        )}
      </div>

      <div className="routing-card">
        <h3>Routing Instructions</h3>
        <form onSubmit={handleSave}>
          <div className="form-group">
            <label htmlFor="instructions">Routing Instructions</label>
            <textarea
              id="instructions"
              rows="12"
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="Enter routing instructions for the classifier..."
            />
            <small>{instructions.length} characters</small>
          </div>

          <div className="form-actions">
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? 'Saving...' : 'Save Routing Configuration'}
            </button>
            <button type="button" className="btn-secondary" onClick={fetchPolicy}>
              Reload Current
            </button>
          </div>
        </form>
      </div>

      {isAdmin && (
        <div className="routing-card">
          <h3>Test Routing Decision</h3>
          <p>
            Runs the real routing engine over a sample prompt and shows exactly what it would
            decide -- no request is dispatched, and nothing is recorded.
          </p>
          {testError && (
            <div className="alert alert-error">
              <strong>Error:</strong> {testError}
              <button onClick={() => setTestError(null)}>&times;</button>
            </div>
          )}
          <form onSubmit={handleTestRouting}>
            <div className="form-group">
              <label htmlFor="test-prompt">Sample Prompt</label>
              <textarea
                id="test-prompt"
                rows="4"
                value={testPrompt}
                onChange={(e) => setTestPrompt(e.target.value)}
                placeholder="Enter a sample request to see how it would be routed..."
              />
            </div>
            <div className="form-group">
              <label htmlFor="test-tool-type">Tool Type (optional)</label>
              <input
                id="test-tool-type"
                type="text"
                value={testToolType}
                onChange={(e) => setTestToolType(e.target.value)}
                placeholder="e.g. code, chat -- leave blank to let the cascade decide"
              />
            </div>
            <div className="form-actions">
              <button
                type="submit"
                className="btn-primary"
                disabled={testLoading || !testPrompt.trim()}
              >
                {testLoading ? 'Running...' : 'Run Test'}
              </button>
            </div>
          </form>

          {testResult && (
            <div className="test-result">
              <p>
                <strong>Model:</strong> {testResult.model}
              </p>
              <p>
                <strong>Tool Type:</strong> {testResult.tool_type} (source:{' '}
                {testResult.tool_type_source})
              </p>
              {testResult.assignment_model && (
                <p>
                  <strong>Assignment Model:</strong> {testResult.assignment_model}
                </p>
              )}
              <p>
                <strong>Capability Veto:</strong> {testResult.capability_veto ? 'Yes' : 'No'}
                {testResult.veto_reason ? ` (${testResult.veto_reason})` : ''}
              </p>
              <p>
                <strong>Escalated:</strong> {testResult.escalated ? 'Yes' : 'No'}
              </p>
              {testResult.routed_from && (
                <p>
                  <strong>Routed From:</strong> {JSON.stringify(testResult.routed_from)}
                </p>
              )}
              {testResult.fallback_chain && testResult.fallback_chain.length > 0 && (
                <p>
                  <strong>Fallback Chain:</strong> {testResult.fallback_chain.join(', ')}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default Routing;
