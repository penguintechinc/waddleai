import { useState, useEffect } from 'react';
import axios from 'axios';
import './Dashboard.css';

function Dashboard() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({
    total_requests: 0,
    total_tokens: 0,
    total_cost: 0,
    active_keys: 0
  });
  const [recentActivity, setRecentActivity] = useState([]);
  const [providerHealth, setProviderHealth] = useState([]);

  useEffect(() => {
    fetchDashboardData();
    const interval = setInterval(fetchDashboardData, 30000);
    return () => clearInterval(interval);
  }, []);

  const fetchDashboardData = async () => {
    try {
      setLoading(true);
      const [summaryRes, statusRes] = await Promise.all([
        axios.get('/api/v1/usage/summary'),
        axios.get('/api/v1/ailb/status')
      ]);

      setStats(summaryRes.data);
      setProviderHealth(statusRes.data.providers || []);

      const activityRes = await axios.get('/api/v1/usage/recent?limit=10');
      setRecentActivity(activityRes.data.activity || []);

      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch dashboard data');
    } finally {
      setLoading(false);
    }
  };

  const formatTimestamp = (timestamp) => {
    if (!timestamp) return 'Unknown';
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffMins < 1440) return `${Math.floor(diffMins / 60)}h ago`;
    return date.toLocaleDateString();
  };

  const getActivityIcon = (type) => {
    switch (type) {
      case 'request':
        return { icon: '📨', className: 'info' };
      case 'error':
        return { icon: '❌', className: 'error' };
      case 'key_created':
        return { icon: '🔑', className: 'success' };
      default:
        return { icon: 'ℹ️', className: 'info' };
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
            <span className="stat-label">Total Requests</span>
          </div>
          <div className="stat-value">{stats.total_requests?.toLocaleString() || 0}</div>
          <div className="stat-subtext">All time</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon green">🔤</div>
            <span className="stat-label">Tokens Processed</span>
          </div>
          <div className="stat-value">
            {stats.total_tokens > 1000000
              ? `${(stats.total_tokens / 1000000).toFixed(2)}M`
              : stats.total_tokens?.toLocaleString() || 0}
          </div>
          <div className="stat-subtext">Total tokens</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon purple">💰</div>
            <span className="stat-label">Total Cost</span>
          </div>
          <div className="stat-value">${(stats.total_cost || 0).toFixed(2)}</div>
          <div className="stat-subtext">Cumulative spending</div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon orange">🔑</div>
            <span className="stat-label">Active Keys</span>
          </div>
          <div className="stat-value">{stats.active_keys || 0}</div>
          <div className="stat-subtext">Virtual keys</div>
        </div>
      </div>

      <div className="dashboard-grid">
        <div className="dashboard-card">
          <h2>Recent Activity</h2>
          {recentActivity.length === 0 ? (
            <div className="empty-state">
              <p>No recent activity</p>
            </div>
          ) : (
            <ul className="activity-list">
              {recentActivity.map((activity, index) => {
                const { icon, className } = getActivityIcon(activity.type);
                return (
                  <li key={index} className="activity-item">
                    <div className={`activity-icon ${className}`}>{icon}</div>
                    <div className="activity-content">
                      <div className="activity-title">
                        {activity.title || activity.model || 'API Request'}
                      </div>
                      <div className="activity-details">
                        {activity.provider && `Provider: ${activity.provider}`}
                        {activity.tokens && ` • ${activity.tokens.toLocaleString()} tokens`}
                        {activity.cost && ` • $${activity.cost.toFixed(4)}`}
                      </div>
                    </div>
                    <div className="activity-time">
                      {formatTimestamp(activity.timestamp)}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="dashboard-card">
          <h2>Provider Health</h2>
          {providerHealth.length === 0 ? (
            <div className="empty-state">
              <p>No providers configured</p>
            </div>
          ) : (
            <ul className="provider-list">
              {providerHealth.map((provider, index) => (
                <li key={index} className="provider-item">
                  <div className="provider-info">
                    <span className="provider-name">{provider.name || provider.provider_type}</span>
                  </div>
                  <span className={`health-badge ${provider.health_status || 'unknown'}`}>
                    {provider.health_status || 'unknown'}
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
