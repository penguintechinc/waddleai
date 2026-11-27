import { useState, useEffect } from 'react';
import axios from 'axios';
import './UsageAnalytics.css';

function normalizeSummary(data) {
  const s = data?.summary;
  const src = s ? s.monthly || s.daily : data;
  return {
    total_requests: (src && (src.requests ?? src.total_requests)) || 0,
    total_tokens: (src && (src.waddleai_tokens ?? src.total_tokens)) || 0,
    total_cost: (src && (src.cost_usd ?? src.total_cost)) || 0,
    daily_requests: (s?.daily && s.daily.requests) || 0,
  };
}

function mapDictToRows(dict, keyField) {
  if (!dict || typeof dict !== 'object') return [];
  if (Array.isArray(dict)) return dict;
  return Object.entries(dict).map(([name, val]) => ({
    [keyField]: name,
    name,
    requests: val.requests || 0,
    tokens: val.tokens || 0,
    cost: val.cost_usd ?? val.cost ?? 0,
  }));
}

function errMsg(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function UsageAnalytics() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState('overview');
  const [days, setDays] = useState(30);
  const [stats, setStats] = useState({
    total_requests: 0,
    total_tokens: 0,
    total_cost: 0,
    daily_requests: 0,
  });
  const [byProvider, setByProvider] = useState([]);
  const [byModel, setByModel] = useState([]);
  const [byUser, setByUser] = useState([]);
  const [byKey, setByKey] = useState([]);
  const [costData, setCostData] = useState(null);
  const [cacheStats, setCacheStats] = useState(null);
  const [exportStatus, setExportStatus] = useState(null);

  useEffect(() => {
    fetchUsageData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchUsageData = async (periodDays = days) => {
    try {
      setLoading(true);
      const params = { days: periodDays };

      const [summaryRes, modelRes, providerRes, userRes, keyRes, costRes, cacheRes] =
        await Promise.all([
          axios.get('/api/v1/usage/summary'),
          axios.get('/api/v1/usage/by-model', { params }),
          axios.get('/api/v1/usage/by-provider', { params }),
          axios.get('/api/v1/usage/by-user', { params }),
          axios.get('/api/v1/usage/by-key', { params }),
          axios.get('/api/v1/usage/cost', { params }),
          axios.get('/api/v1/usage/cache-stats', { params }).catch(() => ({ data: null })),
        ]);

      setStats(normalizeSummary(summaryRes.data));
      setByModel(mapDictToRows(modelRes.data?.by_model, 'model'));
      setByProvider(mapDictToRows(providerRes.data?.by_provider, 'provider'));
      setByUser(
        (userRes.data?.by_user || []).map((u) => ({
          ...u,
          name: u.username || `User #${u.user_id}`,
          requests: u.requests || 0,
          tokens: u.tokens || 0,
          cost: u.cost_usd ?? u.cost ?? 0,
        }))
      );
      setByKey(
        (keyRes.data?.by_key || []).map((k) => ({
          ...k,
          name: k.key_name || k.name || `Key #${k.key_id}`,
          requests: k.requests || 0,
          tokens: k.tokens || 0,
          cost: k.cost_usd ?? k.cost ?? 0,
        }))
      );
      setCostData(costRes.data || null);
      setCacheStats(cacheRes.data?.data || cacheRes.data || null);
      setError(null);
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch usage data'));
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async (format) => {
    try {
      const res = await axios.get('/api/v1/usage/export', { params: { format, days } });
      const content =
        typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2);
      const blob = new Blob([content], {
        type: format === 'csv' ? 'text/csv' : 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `usage-export.${format}`;
      a.click();
      URL.revokeObjectURL(url);
      setExportStatus(`Exported ${format.toUpperCase()} successfully`);
    } catch (err) {
      setError(errMsg(err, 'Failed to export usage data'));
    }
  };

  const renderBarChart = (data, labelField, valueField, title) => {
    if (!data || data.length === 0) {
      return (
        <div className="chart-card">
          <h2>{title}</h2>
          <div className="empty-state">
            <p>No data available</p>
          </div>
        </div>
      );
    }

    const maxValue = Math.max(...data.map((item) => item[valueField] || 0));

    return (
      <div className="chart-card">
        <h2>{title}</h2>
        <div className="chart-container">
          <div className="bar-chart">
            {data.slice(0, 10).map((item, index) => {
              const height = maxValue > 0 ? (item[valueField] / maxValue) * 100 : 0;
              return (
                <div key={index} className="bar-item">
                  <div
                    className="bar"
                    style={{ height: `${height}%` }}
                    title={`${item[labelField]}: ${item[valueField]}`}
                  >
                    <span className="bar-value">
                      {item[valueField] > 1000000
                        ? `${(item[valueField] / 1000000).toFixed(2)}M`
                        : item[valueField] > 1000
                          ? `${(item[valueField] / 1000).toFixed(1)}K`
                          : item[valueField]}
                    </span>
                  </div>
                  <span className="bar-label">{item[labelField]}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    );
  };

  const renderBreakdownTable = (data, title) => {
    if (!data || data.length === 0) {
      return (
        <div className="chart-card">
          <h2>{title}</h2>
          <div className="empty-state">
            <p>No data available</p>
          </div>
        </div>
      );
    }

    const totalCost = data.reduce((sum, item) => sum + (item.cost || 0), 0);

    return (
      <div className="chart-card">
        <h2>{title}</h2>
        <table className="breakdown-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Requests</th>
              <th>Tokens</th>
              <th>Cost</th>
              <th>Share</th>
            </tr>
          </thead>
          <tbody>
            {data.map((item, index) => {
              const costShare = totalCost > 0 ? (item.cost / totalCost) * 100 : 0;
              return (
                <tr key={index}>
                  <td>{item.name}</td>
                  <td>{item.requests.toLocaleString()}</td>
                  <td>{item.tokens.toLocaleString()}</td>
                  <td>${item.cost.toFixed(4)}</td>
                  <td>
                    {costShare.toFixed(1)}%
                    <div className="progress-bar">
                      <div className="progress-fill" style={{ width: `${costShare}%` }}></div>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  };

  if (loading) {
    return <div className="loading">Loading usage analytics...</div>;
  }

  return (
    <div className="usage-analytics">
      <div className="page-header">
        <h1>Usage Analytics</h1>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="btn-secondary" onClick={() => handleExport('csv')}>
            Export CSV
          </button>
          <button className="btn-secondary" onClick={() => handleExport('json')}>
            Export JSON
          </button>
        </div>
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      {exportStatus && (
        <div className="alert alert-success">
          {exportStatus}
          <button onClick={() => setExportStatus(null)}>&times;</button>
        </div>
      )}

      <div className="filters-bar">
        <div className="filters-row">
          <div className="filter-group">
            <label htmlFor="usage-period-days">Period (Days)</label>
            <select
              id="usage-period-days"
              value={days}
              onChange={(e) => {
                const next = Number(e.target.value);
                setDays(next);
                fetchUsageData(next);
              }}
            >
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>
          <div className="filter-group">
            <label>&nbsp;</label>
            <button className="btn-primary" onClick={() => fetchUsageData(days)}>
              Refresh
            </button>
          </div>
        </div>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon blue">📊</div>
            <span className="stat-label">Monthly Requests</span>
          </div>
          <div className="stat-value">{stats.total_requests.toLocaleString()}</div>
          <div className="stat-subtext">Today: {stats.daily_requests.toLocaleString()}</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon green">🔤</div>
            <span className="stat-label">Total Tokens</span>
          </div>
          <div className="stat-value">
            {stats.total_tokens > 1000000
              ? `${(stats.total_tokens / 1000000).toFixed(2)}M`
              : stats.total_tokens.toLocaleString()}
          </div>
          <div className="stat-subtext">Monthly total</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon purple">💰</div>
            <span className="stat-label">Total Cost</span>
          </div>
          <div className="stat-value">${stats.total_cost.toFixed(2)}</div>
          <div className="stat-subtext">Monthly spending</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon orange">⚡</div>
            <span className="stat-label">Cache Hit Rate</span>
          </div>
          <div className="stat-value">
            {cacheStats ? `${((cacheStats.hit_rate || 0) * 100).toFixed(1)}%` : 'N/A'}
          </div>
          <div className="stat-subtext">
            {cacheStats ? `${(cacheStats.tokens_saved_total || 0).toLocaleString()} tokens saved` : 'No cache data'}
          </div>
        </div>
      </div>

      <div className="tabs" role="tablist">
        {[
          { id: 'overview', label: 'Overview' },
          { id: 'users', label: 'By User' },
          { id: 'keys', label: 'By Key' },
          { id: 'cost', label: 'Cost Analytics' },
          { id: 'cache', label: 'Cache Stats' },
        ].map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'overview' && (
        <div className="charts-section">
          {renderBarChart(byProvider, 'provider', 'tokens', 'Token Usage by Provider')}
          {renderBarChart(byModel, 'model', 'requests', 'Requests by Model')}
          {renderBreakdownTable(byProvider, 'Cost Breakdown by Provider')}
          {renderBreakdownTable(byModel, 'Cost Breakdown by Model')}
        </div>
      )}

      {activeTab === 'users' && (
        <div className="charts-section">{renderBreakdownTable(byUser, 'Usage by User')}</div>
      )}

      {activeTab === 'keys' && (
        <div className="charts-section">{renderBreakdownTable(byKey, 'Usage by Virtual Key')}</div>
      )}

      {activeTab === 'cost' && (
        <div className="chart-card">
          <h2>Cost Analytics ({costData?.period_days ?? days} days)</h2>
          {!costData ? (
            <div className="empty-state">
              <p>No cost data available</p>
            </div>
          ) : (
            <div>
              <p>Total Cost: ${(costData.total_cost_usd ?? 0).toFixed(4)}</p>
              <p>Avg Daily Cost: ${(costData.avg_daily_cost_usd ?? 0).toFixed(4)}</p>
              <p>Projected Monthly Cost: ${(costData.projected_monthly_cost_usd ?? 0).toFixed(4)}</p>
            </div>
          )}
        </div>
      )}

      {activeTab === 'cache' && (
        <div className="chart-card">
          <h2>Cache Statistics</h2>
          {!cacheStats ? (
            <div className="empty-state">
              <p>No cache statistics available</p>
            </div>
          ) : (
            <div>
              <p>Total Requests: {(cacheStats.total_requests ?? 0).toLocaleString()}</p>
              <p>Hit Rate: {((cacheStats.hit_rate ?? 0) * 100).toFixed(1)}%</p>
              <p>Tokens Saved: {(cacheStats.tokens_saved_total ?? 0).toLocaleString()}</p>
              <p>Estimated USD Saved: ${(cacheStats.usd_saved_estimate ?? 0).toFixed(4)}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default UsageAnalytics;
