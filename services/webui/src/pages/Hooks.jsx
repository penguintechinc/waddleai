import { useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import HookRulesTab from '../components/HookRulesTab';
import HookDenylistTab from '../components/HookDenylistTab';
import HookConfigTab from '../components/HookConfigTab';
import HookVisibilityTab from '../components/HookVisibilityTab';
import './Hooks.css';

const TABS = [
  { id: 'rules', label: 'Rules' },
  { id: 'denylist', label: 'Denylist' },
  { id: 'config', label: 'Config' },
  { id: 'visibility', label: 'Visibility' },
];

// WebUI surface for spec §18 (agent-ecosystem hooks), backed by
// `services/management/app/api/v1/hooks.py` + `hook_rules.py` +
// `hook_metrics.py` -- this page is a client of that already-shipped API,
// not a reimplementation of its authorization or scoping logic. `admin`
// (global) and `resource_manager` (org-scoped tenant admin) are the only
// roles the server accepts on any hooks route; anyone else gets a 403 from
// the API itself, so this page shows a plain notice instead of even
// attempting the fetch.
function Hooks() {
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';
  const isResourceManager = user?.role === 'resource_manager';
  const organizationId = user?.organization_id;

  const [activeTab, setActiveTab] = useState('rules');
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const handleError = (message) => setError(message);
  const handleSuccess = (message) => {
    setSuccess(message);
    setError(null);
    setTimeout(() => setSuccess(null), 3000);
  };

  if (!isAdmin && !isResourceManager) {
    return (
      <div className="hooks">
        <div className="page-header">
          <h1>Agent Hooks</h1>
        </div>
        <div className="admin-required-notice">
          Managing agent hooks requires Admin or Resource Manager access.
        </div>
      </div>
    );
  }

  const tabProps = { isAdmin, organizationId, onError: handleError, onSuccess: handleSuccess };

  return (
    <div className="hooks">
      <div className="page-header">
        <h1>Agent Hooks</h1>
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

      <div className="hooks-tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`hooks-tab ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="hooks-tab-panel">
        {activeTab === 'rules' && <HookRulesTab {...tabProps} />}
        {activeTab === 'denylist' && <HookDenylistTab {...tabProps} />}
        {activeTab === 'config' && <HookConfigTab {...tabProps} />}
        {activeTab === 'visibility' && <HookVisibilityTab {...tabProps} />}
      </div>
    </div>
  );
}

export default Hooks;
