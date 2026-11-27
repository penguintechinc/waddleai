import { useState, useEffect, useCallback, useMemo } from 'react';
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
function errMsg(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function Routing() {
  const { user } = useAuth();
  const organizationId = user?.organization_id;
  const isAdmin = user?.role === 'admin';

  const [instructions, setInstructions] = useState('');
  const [activeTab, setActiveTab] = useState('policy');
  const [rules, setRules] = useState([]);
  const [ruleForm, setRuleForm] = useState({ name: '', priority: 100, match: '{}', action: '{}', enabled: true });
  const [aliases, setAliases] = useState([]);
  const [aliasForm, setAliasForm] = useState({ source_model: '', target_model: '', target_provider: '', enabled: true });
  const [accessPolicies, setAccessPolicies] = useState([]);
  const [policyForm, setPolicyForm] = useState({
    scope_type: 'org',
    scope_ref: '',
    model_pattern: '',
    action: 'deny',
    fallback_model: '',
    reason: '',
    enabled: true,
  });
  const [decisions, setDecisions] = useState([]);
  const [traceId, setTraceId] = useState('');
  const [traceDetail, setTraceDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // "Routing LLM Model" -- the routing-classifier tool_type's model_assignments
  // row (spec §7.2, §2.3). Fixed Gemma 4 option list rather than the model
  // registry: this assignment is pinned to the Gemma 4 family specifically
  // (shared/routing/classifier.py: "gemma4:e4b default, no dual-default
  // alternative required"), unlike ordinary tool-type assignments (chat,
  // code, ...) which can point at any capable model. There is also no live
  // model-registry endpoint yet -- migration 008 (model_registry) hasn't
  // landed (shared/routing/offers.py docstring) -- so even a registry-backed
  // dropdown isn't available to source options from today. Valid Gemma 4
  // tags are e2b/e4b/12b/26b/31b -- there is no "2b" tag and "gemma4:2b"
  // is unpullable; Gemma 3 and PRC-origin models are never offered here.
  // e2b is deliberately absent: it tested below the usable bar for stage-2
  // classification (2026-09-07, migration 019), so e4b is the supported
  // minimum. A deployment still pinned to a below-minimum tag keeps showing
  // it (see legacy option below) but cannot re-select it once changed.
  const GEMMA4_MODEL_OPTIONS = [
    { value: 'gemma4:e4b', label: 'Gemma 4 E4B (default)' },
    { value: 'gemma4:12b', label: 'Gemma 4 12B' },
    { value: 'gemma4:26b', label: 'Gemma 4 26B' },
    { value: 'gemma4:31b', label: 'Gemma 4 31B' },
  ];
  const [routingModel, setRoutingModel] = useState('gemma4:e4b');
  const [routingModelId, setRoutingModelId] = useState(null);
  const [routingModelLoading, setRoutingModelLoading] = useState(true);
  const [routingModelSaving, setRoutingModelSaving] = useState(false);

  // A deployment saved before the e4b minimum can still be pinned to a tag the
  // list no longer offers (e2b). Showing only the supported options would make
  // the <select> silently render the wrong model as current, so the stored
  // value is appended as a disabled legacy entry: visible, but not re-selectable
  // once the admin moves off it.
  const routingModelOptions = useMemo(() => {
    if (GEMMA4_MODEL_OPTIONS.some((option) => option.value === routingModel)) {
      return GEMMA4_MODEL_OPTIONS;
    }
    return [
      ...GEMMA4_MODEL_OPTIONS,
      {
        value: routingModel,
        label: `${routingModel} (unsupported \u2014 below the Gemma 4 E4B minimum)`,
        disabled: true,
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routingModel]);

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
      setError(errMsg(err, 'Failed to fetch routing policy'));
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
        setRoutingModel(entry.model_name || 'gemma4:e4b');
        setRoutingModelId(entry.id ?? null);
      }
    } catch (err) {
      const message = errMsg(err, 'Failed to fetch routing LLM model');
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
      setError(errMsg(err, 'Failed to save routing configuration'));
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
      setError(errMsg(err, 'Failed to save routing LLM model'));
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
      setTestError(errMsg(err, 'Failed to run routing test'));
      setTestResult(null);
    } finally {
      setTestLoading(false);
    }
  };

  const handleDeletePolicy = async () => {
    try {
      await axios.delete(`/api/v1/routing/policies/${organizationId}`);
      setInstructions('');
      setSuccess('Routing policy deleted successfully');
      setError(null);
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to delete routing policy'));
    }
  };

  const handleSeedAssignments = async () => {
    try {
      await axios.post('/api/v1/routing/assignments/seed');
      setSuccess('Default routing assignments seeded');
      setError(null);
      fetchRoutingModel();
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(errMsg(err, 'Failed to seed routing assignments'));
    }
  };

  const fetchTabData = async (tab) => {
    try {
      if (tab === 'rules') {
        const res = await axios.get('/api/v1/routing/rules/');
        setRules(res.data?.data || []);
      } else if (tab === 'aliases') {
        const res = await axios.get('/api/v1/routing/aliases/');
        setAliases(res.data?.data || []);
      } else if (tab === 'access') {
        const res = await axios.get('/api/v1/routing/access-policies/');
        setAccessPolicies(res.data?.data || []);
      } else if (tab === 'decisions') {
        const res = await axios.get('/api/v1/routing/decisions/');
        setDecisions(res.data?.data || []);
      }
    } catch (err) {
      setError(errMsg(err, `Failed to load ${tab}`));
    }
  };

  const handleTabSelect = (tab) => {
    setActiveTab(tab);
    if (tab !== 'policy') {
      fetchTabData(tab);
    }
  };

  const handleCreateRule = async (e) => {
    e.preventDefault();
    try {
      const payload = {
        name: ruleForm.name,
        priority: Number(ruleForm.priority) || 100,
        match: JSON.parse(ruleForm.match || '{}'),
        action: JSON.parse(ruleForm.action || '{}'),
        enabled: ruleForm.enabled,
      };
      await axios.post('/api/v1/routing/rules/', payload);
      setRuleForm({ name: '', priority: 100, match: '{}', action: '{}', enabled: true });
      setSuccess('Routing rule created');
      fetchTabData('rules');
    } catch (err) {
      setError(errMsg(err, err.message || 'Failed to create routing rule'));
    }
  };

  const handleToggleRule = async (rule) => {
    try {
      await axios.put(`/api/v1/routing/rules/${rule.id}`, { enabled: !rule.enabled });
      fetchTabData('rules');
    } catch (err) {
      setError(errMsg(err, 'Failed to update routing rule'));
    }
  };

  const handleDeleteRule = async (id) => {
    try {
      await axios.delete(`/api/v1/routing/rules/${id}`);
      fetchTabData('rules');
    } catch (err) {
      setError(errMsg(err, 'Failed to delete routing rule'));
    }
  };

  const handleCreateAlias = async (e) => {
    e.preventDefault();
    try {
      await axios.post('/api/v1/routing/aliases/', aliasForm);
      setAliasForm({ source_model: '', target_model: '', target_provider: '', enabled: true });
      setSuccess('Model alias created');
      fetchTabData('aliases');
    } catch (err) {
      setError(errMsg(err, 'Failed to create model alias'));
    }
  };

  const handleToggleAlias = async (alias) => {
    try {
      await axios.put(`/api/v1/routing/aliases/${alias.id}`, { enabled: !alias.enabled });
      fetchTabData('aliases');
    } catch (err) {
      setError(errMsg(err, 'Failed to update model alias'));
    }
  };

  const handleDeleteAlias = async (id) => {
    try {
      await axios.delete(`/api/v1/routing/aliases/${id}`);
      fetchTabData('aliases');
    } catch (err) {
      setError(errMsg(err, 'Failed to delete model alias'));
    }
  };

  const handleCreateAccessPolicy = async (e) => {
    e.preventDefault();
    try {
      await axios.post('/api/v1/routing/access-policies/', policyForm);
      setPolicyForm({
        scope_type: 'org',
        scope_ref: '',
        model_pattern: '',
        action: 'deny',
        fallback_model: '',
        reason: '',
        enabled: true,
      });
      setSuccess('Access policy created');
      fetchTabData('access');
    } catch (err) {
      setError(errMsg(err, 'Failed to create access policy'));
    }
  };

  const handleToggleAccessPolicy = async (policy) => {
    try {
      await axios.put(`/api/v1/routing/access-policies/${policy.id}`, { enabled: !policy.enabled });
      fetchTabData('access');
    } catch (err) {
      setError(errMsg(err, 'Failed to update access policy'));
    }
  };

  const handleDeleteAccessPolicy = async (id) => {
    try {
      await axios.delete(`/api/v1/routing/access-policies/${id}`);
      fetchTabData('access');
    } catch (err) {
      setError(errMsg(err, 'Failed to delete access policy'));
    }
  };

  const handleLookupTrace = async (e) => {
    e.preventDefault();
    if (!traceId.trim()) return;
    try {
      const res = await axios.get(`/api/v1/routing/decisions/${encodeURIComponent(traceId.trim())}`);
      setTraceDetail(res.data?.data || res.data);
    } catch (err) {
      setError(errMsg(err, 'Trace not found'));
      setTraceDetail(null);
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

      <div className="tabs" role="tablist">
        {[
          { id: 'policy', label: 'Policy & Classifier' },
          { id: 'rules', label: 'Routing Rules' },
          { id: 'aliases', label: 'Model Aliases' },
          { id: 'access', label: 'Access Policies' },
          { id: 'decisions', label: 'Decisions' },
        ].map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => handleTabSelect(tab.id)}
          >
            {tab.label}
          </button>
        ))}
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

      {activeTab === 'policy' && (
        <>
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
                  {routingModelOptions.map((option) => (
                    <option key={option.value} value={option.value} disabled={option.disabled}>
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
                <button type="button" className="btn-secondary" onClick={handleSeedAssignments}>
                  Seed Default Assignments
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
            {isAdmin && (
              <button type="button" className="btn-danger" onClick={handleDeletePolicy}>
                Delete Policy
              </button>
            )}
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
        </>
      )}

      {activeTab === 'rules' && (
        <div className="routing-card">
          <h3>Routing Rules</h3>
          <form onSubmit={handleCreateRule} className="routing-subform">
            <input
              type="text"
              aria-label="Rule Name"
              placeholder="Rule name"
              value={ruleForm.name}
              onChange={(e) => setRuleForm({ ...ruleForm, name: e.target.value })}
              required
            />
            <input
              type="number"
              aria-label="Rule Priority"
              value={ruleForm.priority}
              onChange={(e) => setRuleForm({ ...ruleForm, priority: e.target.value })}
            />
            <input
              type="text"
              aria-label="Rule Match JSON"
              value={ruleForm.match}
              onChange={(e) => setRuleForm({ ...ruleForm, match: e.target.value })}
            />
            <input
              type="text"
              aria-label="Rule Action JSON"
              value={ruleForm.action}
              onChange={(e) => setRuleForm({ ...ruleForm, action: e.target.value })}
            />
            <button type="submit" className="btn-primary">Add Rule</button>
          </form>
          <ul>
            {rules.map((r) => (
              <li key={r.id}>
                <span>{r.name} (priority {r.priority}) — {r.enabled ? 'Enabled' : 'Disabled'}</span>
                <button className="btn-small btn-secondary" onClick={() => handleToggleRule(r)}>
                  {r.enabled ? 'Disable' : 'Enable'}
                </button>
                <button className="btn-small btn-danger" onClick={() => handleDeleteRule(r.id)}>
                  Delete Rule
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeTab === 'aliases' && (
        <div className="routing-card">
          <h3>Model Aliases</h3>
          <form onSubmit={handleCreateAlias} className="routing-subform">
            <input
              type="text"
              aria-label="Source Model"
              placeholder="Source model"
              value={aliasForm.source_model}
              onChange={(e) => setAliasForm({ ...aliasForm, source_model: e.target.value })}
              required
            />
            <input
              type="text"
              aria-label="Target Model"
              placeholder="Target model"
              value={aliasForm.target_model}
              onChange={(e) => setAliasForm({ ...aliasForm, target_model: e.target.value })}
              required
            />
            <input
              type="text"
              aria-label="Target Provider"
              placeholder="Provider (optional)"
              value={aliasForm.target_provider}
              onChange={(e) => setAliasForm({ ...aliasForm, target_provider: e.target.value })}
            />
            <button type="submit" className="btn-primary">Add Alias</button>
          </form>
          <ul>
            {aliases.map((a) => (
              <li key={a.id}>
                <span>{a.source_model} → {a.target_model}</span>
                <button className="btn-small btn-secondary" onClick={() => handleToggleAlias(a)}>
                  {a.enabled ? 'Disable' : 'Enable'}
                </button>
                <button className="btn-small btn-danger" onClick={() => handleDeleteAlias(a.id)}>
                  Delete Alias
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeTab === 'access' && (
        <div className="routing-card">
          <h3>Model Access Policies</h3>
          <form onSubmit={handleCreateAccessPolicy} className="routing-subform">
            <select
              aria-label="Scope Type"
              value={policyForm.scope_type}
              onChange={(e) => setPolicyForm({ ...policyForm, scope_type: e.target.value })}
            >
              <option value="org">org</option>
              <option value="team">team</option>
              <option value="user">user</option>
            </select>
            <input
              type="text"
              aria-label="Scope Ref"
              placeholder="Scope ref"
              value={policyForm.scope_ref}
              onChange={(e) => setPolicyForm({ ...policyForm, scope_ref: e.target.value })}
            />
            <input
              type="text"
              aria-label="Model Pattern"
              placeholder="Model pattern (e.g. gpt-4*)"
              value={policyForm.model_pattern}
              onChange={(e) => setPolicyForm({ ...policyForm, model_pattern: e.target.value })}
              required
            />
            <select
              aria-label="Policy Action"
              value={policyForm.action}
              onChange={(e) => setPolicyForm({ ...policyForm, action: e.target.value })}
            >
              <option value="deny">deny</option>
              <option value="allow">allow</option>
              <option value="fallback">fallback</option>
            </select>
            <button type="submit" className="btn-primary">Add Access Policy</button>
          </form>
          <ul>
            {accessPolicies.map((p) => (
              <li key={p.id}>
                <span>{p.scope_type}:{p.scope_ref || '*'} — {p.model_pattern} ({p.action})</span>
                <button className="btn-small btn-secondary" onClick={() => handleToggleAccessPolicy(p)}>
                  {p.enabled ? 'Disable' : 'Enable'}
                </button>
                <button className="btn-small btn-danger" onClick={() => handleDeleteAccessPolicy(p.id)}>
                  Delete Access Policy
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeTab === 'decisions' && (
        <div className="routing-card">
          <h3>Routing Decisions</h3>
          <form onSubmit={handleLookupTrace} className="routing-subform">
            <input
              type="text"
              aria-label="Request ID"
              placeholder="Lookup request_id"
              value={traceId}
              onChange={(e) => setTraceId(e.target.value)}
            />
            <button type="submit" className="btn-primary">Lookup Trace</button>
          </form>
          {traceDetail && <pre>{JSON.stringify(traceDetail, null, 2)}</pre>}
          <ul>
            {decisions.map((d, idx) => (
              <li key={d.request_id || idx}>
                {d.request_id} — {d.selected_model || d.model} ({d.tool_type})
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default Routing;
