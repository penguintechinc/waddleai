import { useState } from 'react';
import axios from 'axios';

// Self-service OpenCode config renderer (spec §11.3), split out of
// Integrations.jsx to keep that file under the project's 25,000-character
// limit. Fully self-contained: the virtual key never leaves this
// component's local state, is never logged, and is cleared immediately
// after the single request that needs it.
function OpenCodeConfigCard() {
  const [virtualKey, setVirtualKey] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const handleGenerateConfig = async (e) => {
    e.preventDefault();
    if (!virtualKey.trim()) {
      setError('Paste one of your own virtual keys to generate a config');
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const response = await axios.post('/api/v1/integrations/opencode-config', {
        virtual_key: virtualKey.trim(),
      });
      setResult(response.data.data);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to generate OpenCode config');
      setResult(null);
    } finally {
      // Never keep the raw key in state longer than the single request needs.
      setVirtualKey('');
      setLoading(false);
    }
  };

  const handleDownloadConfig = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'opencode.json';
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleCopyConfig = () => {
    if (!result) return;
    navigator.clipboard
      .writeText(JSON.stringify(result, null, 2))
      .then(() => {
        setNotice('Config copied to clipboard!');
        setTimeout(() => setNotice(null), 2000);
      })
      .catch(() => {
        setError('Failed to copy config to clipboard');
      });
  };

  return (
    <section className="integrations-card">
      <h3>OpenCode Config</h3>
      <p>
        Render an <code>opencode.json</code> for one of your own virtual keys: a custom provider
        pointed at this deployment&apos;s <code>/v1</code>, plus an MCP entry pointed at{' '}
        <code>/mcp</code>.
      </p>
      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}
      {notice && (
        <div className="alert alert-success">
          <strong>Success:</strong> {notice}
          <button onClick={() => setNotice(null)}>&times;</button>
        </div>
      )}
      <form onSubmit={handleGenerateConfig}>
        <div className="form-group">
          <label htmlFor="config-virtual-key">Virtual Key</label>
          <input
            id="config-virtual-key"
            type="password"
            value={virtualKey}
            onChange={(e) => setVirtualKey(e.target.value)}
            placeholder="wk-..."
            autoComplete="off"
          />
          <small>
            Never stored -- sent once in the request body to render your config, then discarded
            from this form.
          </small>
        </div>
        <div className="form-actions">
          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? 'Generating...' : 'Generate Config'}
          </button>
        </div>
      </form>

      {result && (
        <div className="config-result">
          <p className="warning-text">
            This file contains your API key in plaintext. Store it securely and never commit it
            to version control.
          </p>
          <pre data-testid="opencode-config-preview">{JSON.stringify(result, null, 2)}</pre>
          <div className="form-actions">
            <button type="button" className="btn-primary" onClick={handleDownloadConfig}>
              Download opencode.json
            </button>
            <button type="button" className="btn-secondary" onClick={handleCopyConfig}>
              Copy to Clipboard
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

export default OpenCodeConfigCard;
