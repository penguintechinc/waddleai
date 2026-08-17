import { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { useAuth } from '../contexts/AuthContext';
import './Routing.css';

// Thin presentation layer over /api/v1/routing/policies/<organization_id>.
// Ported from the legacy management plane's /routing-config admin page, then
// repointed from the retired /api/v1/routing-matrix/{instructions,test}
// endpoints to the routing_policies CRUD (spec §7.1/§7.3): classifier_prompt
// absorbs the legacy Valkey routing:instructions natural-language routing
// UX, so that's the only field this screen manages. The legacy page's
// "Routing LLM Model" selector and "Test Routing Decision" tool have no
// equivalent here -- routing-classifier model selection now lives on the
// model_assignments admin surface (not yet built in this webui), and there
// is no dry-run/simulate endpoint (routing_decision_traces is a read-only
// log of real, already-dispatched requests) -- both controls are dropped
// rather than left calling a dead endpoint. All persistence/validation is
// server-side.
function Routing() {
  const { user } = useAuth();
  const organizationId = user?.organization_id;

  const [instructions, setInstructions] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

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

  useEffect(() => {
    if (organizationId) {
      fetchPolicy();
    }
  }, [organizationId, fetchPolicy]);

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
    </div>
  );
}

export default Routing;
