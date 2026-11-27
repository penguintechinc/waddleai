import { useState, useEffect } from 'react';
import axios from 'axios';
import './Dashboard.css';

function normalizeSummary(data) {
  const s = data?.summary;
  const src = s ? s.monthly || s.daily : data;
  return {
    total_requests: (src && (src.requests ?? src.total_requests)) || 0,
    total_tokens: (src && (src.waddleai_tokens ?? src.total_tokens)) || 0,
    total_cost: (src && (src.cost_usd ?? src.total_cost)) || 0,
    daily_requests: (s?.daily && s.daily.requests) || 0,
    daily_tokens: (s?.daily && s.daily.waddleai_tokens) || 0,
  };
}

function errMsg(err, fallback) {
  return err?.response?.data?.error || fallback;
}

function Dashboard() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({
    total_requests: 0,
    total_tokens: 0,
    total_cost: 0,
    daily_requests: 0,
    daily_tokens: 0,
  });
  const [modelUsage, setModelUsage] = useState([]);
  const [fleetBackends, setFleetBackends] = useState([]);
  const [ciliumStatus, setCiliumStatus] = useState(null);

  useEffect(() => {
    fetchDashboardData();
    const interval = setInterval(fetchDashboardData, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchDashboardData = async () => {
    try {
      setLoading(true);
      const [summaryRes, byModelRes, backendsRes, ciliumRes] = await Promise.all([
        axios.get('/api/v1/usage/summary'),
        axios.get('/api/v1/usage/by-model'),
        axios.get('/api/v1/fleet/backends').catch((err) => {
          const status = err?.response?.status;
          if (status === 403 || status === 404) return { data: { data: [] } };
          throw err;
        }),
        axios.get('/api/v1/cilium/status').catch((err) => {
          const status = err?.response?.status;
          if (status === 403 || status === 404) return { data: null };
          throw err;
        }),
      ]);

      setStats(normalizeSummary(summaryRes.data));

      const byModelObj = byModelRes.data?.by_model || {};
      const models = Object.entries(byModelObj)
        .map(([model, metrics]) => ({ model, ...metrics }))
        .sort((a, b) => (b.tokens || 0) - (a.tokens || 0));
      setModelUsage(models);

      setFleetBackends(backendsRes.data?.data || []);
      setCiliumStatus(ciliumRes.data || null);
      setError(null);
    } catch (err) {
      setError(errMsg(err, 'Failed to fetch dashboard data'));
    } finally {
      setLoading(false);
    }
  };

  if (loading && stats.total_requests === 0) {
    return <div className="loading">Loading dashboard...</div>;
  }

  return (
    <div className="dashboard">
      <div className="page-header">
        <h1>Dashboard</h1>
        <p>Welcome to WaddleAI - AI Gateway Management Platform</p>
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      <div className="quick-stats">
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
            <span className="stat-label">Tokens Processed</span>
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
            <div className="stat-icon orange">🛡️</div>
            <span className="stat-label">Cilium Policy</span>
          </div>
          <div className="stat-value">
            {ciliumStatus
              ? ciliumStatus.degraded
                ? 'Degraded'
                : ciliumStatus.flag_enabled
                  ? 'Active'
                  : 'Disabled'
              : 'N/A'}
          </div>
          <div className="stat-subtext">
            {ciliumStatus ? `${(ciliumStatus.applied || []).length} applied` : 'Unavailable'}
          </div>
        </div>
      </div>

      <div className="dashboard-grid">
        <div className="dashboard-card">
          <h2>Usage by Model</h2>
          {modelUsage.length === 0 ? (
            <div className="empty-state">
              <p>No model usage recorded</p>
            </div>
          ) : (
            <ul className="activity-list">
              {modelUsage.map((item) => (
                <li key={item.model} className="activity-item">
                  <div className="activity-icon info">🤖</div>
                  <div className="activity-content">
                    <div className="activity-title">{item.model}</div>
                    <div className="activity-details">
                      {(item.requests || 0).toLocaleString()} requests •{' '}
                      {(item.tokens || 0).toLocaleString()} tokens • $
                      {(item.cost_usd || 0).toFixed(4)}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="dashboard-card">
          <h2>Fleet Backends</h2>
          {fleetBackends.length === 0 ? (
            <div className="empty-state">
              <p>No fleet backends configured</p>
            </div>
          ) : (
            <ul className="provider-list">
              {fleetBackends.map((backend) => (
                <li key={backend.id || backend.name} className="provider-item">
                  <div className="provider-info">
                    <span className="provider-name">{backend.name}</span>
                  </div>
                  <span className={`health-badge ${backend.status || 'unknown'}`}>
                    {backend.status || 'unknown'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

export default Dashboard;
