import { useState, useEffect } from 'react';
import axios from 'axios';
import './Routing.css';

// Thin presentation layer over /api/v1/routing-matrix/{instructions,test}.
// Ported from the legacy management plane's /routing-config admin page --
// configures the routing LLM's freeform natural-language instructions and
// selected model (Redis-backed), and exercises the test-routing-decision
// endpoint. No business logic lives here; all validation/persistence is
// server-side.
function Routing() {
  const [instructions, setInstructions] = useState('');
  const [routingLlm, setRoutingLlm] = useState('llama3.2:1b');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [testPrompt, setTestPrompt] = useState('');
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState(null);

  useEffect(() => {
    fetchInstructions();
  }, []);

  const fetchInstructions = async () => {
    try {
      setLoading(true);
      const response = await axios.get('/api/v1/routing-matrix/instructions');
      const data = response.data.data || {};
      setInstructions(data.instructions || '');
      setRoutingLlm(data.routing_llm || 'llama3.2:1b');
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch routing instructions');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async (e) => {
    e.preventDefault();
    try {
      setSaving(true);
      await axios.post('/api/v1/routing-matrix/instructions', {
        instructions,
        routing_llm: routingLlm,
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

  const handleTest = async (e) => {
    e.preventDefault();
    try {
      setTesting(true);
      setTestError(null);
      const response = await axios.post('/api/v1/routing-matrix/test', { prompt: testPrompt });
      setTestResult(response.data.data);
    } catch (err) {
      setTestError(err.response?.data?.error || 'Routing test failed');
      setTestResult(null);
    } finally {
      setTesting(false);
    }
  };

  if (loading) {
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
            <label htmlFor="routing-llm">Routing LLM Model</label>
            <select
              id="routing-llm"
              value={routingLlm}
              onChange={(e) => setRoutingLlm(e.target.value)}
            >
              <option value="llama3.2:1b">llama3.2:1b (Fast, Local)</option>
              <option value="llama3.2:3b">llama3.2:3b (Balanced)</option>
              <option value="o1-mini">o1-mini (Advanced Reasoning)</option>
              <option value="gpt-4o-mini">gpt-4o-mini (Cloud, Accurate)</option>
            </select>
            <small>Choose a fast, efficient model for routing decisions</small>
          </div>

          <div className="form-group">
            <label htmlFor="instructions">Routing Instructions</label>
            <textarea
              id="instructions"
              rows="12"
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="Enter routing instructions for the LLM..."
            />
            <small>{instructions.length} characters</small>
          </div>

          <div className="form-actions">
            <button type="submit" className="btn-primary" disabled={saving}>
              {saving ? 'Saving...' : 'Save Routing Configuration'}
            </button>
            <button type="button" className="btn-secondary" onClick={fetchInstructions}>
              Reload Current
            </button>
          </div>
        </form>
      </div>

      <div className="routing-card">
        <h3>Test Routing Decision</h3>
        <form onSubmit={handleTest}>
          <div className="form-group">
            <label htmlFor="test-prompt">Test Prompt</label>
            <textarea
              id="test-prompt"
              rows="4"
              value={testPrompt}
              onChange={(e) => setTestPrompt(e.target.value)}
              placeholder="Enter a test prompt to see how it would be routed..."
            />
          </div>
          <button type="submit" className="btn-primary" disabled={testing || !testPrompt}>
            {testing ? 'Testing...' : 'Test Routing'}
          </button>
        </form>

        {testError && (
          <div className="alert alert-error">
            <strong>Error:</strong> {testError}
          </div>
        )}

        {testResult && (
          <div className="test-result">
            <p><strong>Routing Decision:</strong> {testResult.routing_decision}</p>
            <p><strong>Request Type:</strong> {testResult.request_type}</p>
            <p><strong>Confidence:</strong> {(testResult.confidence * 100).toFixed(1)}%</p>
            <p><strong>Reasoning:</strong> {testResult.routing_reasoning}</p>
            <p><strong>Alternative Models:</strong> {(testResult.alternative_models || []).join(', ')}</p>
          </div>
        )}
      </div>
    </div>
  );
}

export default Routing;
