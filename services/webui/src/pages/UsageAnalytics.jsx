import { useState, useEffect } from 'react';
import axios from 'axios';
import './UsageAnalytics.css';

function UsageAnalytics() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({
    total_requests: 0,
    total_tokens: 0,
    total_cost: 0,
    active_keys: 0
  });
  const [usageData, setUsageData] = useState({
    by_provider: [],
    by_model: [],
    by_user: [],
    timeline: []
  });
  const [filters, setFilters] = useState({
    start_date: '',
    end_date: '',
    provider: '',
    model: ''
  });

  useEffect(() => {
    fetchUsageData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchUsageData = async () => {
    try {
      setLoading(true);
      const params = {};
      if (filters.start_date) params.start_date = filters.start_date;
      if (filters.end_date) params.end_date = filters.end_date;
      if (filters.provider) params.provider = filters.provider;
      if (filters.model) params.model = filters.model;

      const [summaryRes, usageRes] = await Promise.all([
        axios.get('/api/v1/usage/summary', { params }),
        axios.get('/api/v1/usage', { params })
      ]);

      setStats(summaryRes.data);
      setUsageData(usageRes.data);
      setError(null);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to fetch usage data');
    } finally {
      setLoading(false);
    }
  };

  const handleFilterChange = (field, value) => {
    setFilters({ ...filters, [field]: value });
  };

  const applyFilters = () => {
    fetchUsageData();
  };

  const calculateChange = (current, previous) => {
    if (!previous || previous === 0) return 0;
    return ((current - previous) / previous * 100).toFixed(1);
  };

  const getMaxValue = (data, field) => {
    if (!data || data.length === 0) return 0;
    return Math.max(...data.map(item => item[field] || 0));
  };

  const renderBarChart = (data, labelField, valueField, title) => {
    if (!data || data.length === 0) {
      return <div className="empty-state"><p>No data available</p></div>;
    }

    const maxValue = getMaxValue(data, valueField);

    return (
      <div className="chart-card">
        <h2>{title}</h2>
        <div className="chart-container">
          <div className="bar-chart">
            {data.slice(0, 10).map((item, index) => {
              const height = maxValue > 0 ? (item[valueField] / maxValue * 100) : 0;
              return (
                <div key={index} className="bar-item">
                  <div
                    className="bar"
                    style={{ height: `${height}%` }}
                    title={`${item[labelField]}: ${item[valueField]}`}
                  >
                    <span className="bar-value">
                      {typeof item[valueField] === 'number' && item[valueField] > 1000000
                        ? `${(item[valueField] / 1000000).toFixed(2)}M`
                        : typeof item[valueField] === 'number' && item[valueField] > 1000
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
          <div className="empty-state"><p>No data available</p></div>
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
              const costShare = totalCost > 0 ? (item.cost / totalCost * 100) : 0;
              return (
                <tr key={index}>
                  <td>{item.name || item.provider || item.model || 'Unknown'}</td>
                  <td>{item.requests?.toLocaleString() || 0}</td>
                  <td>{item.tokens?.toLocaleString() || 0}</td>
                  <td>${(item.cost || 0).toFixed(4)}</td>
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
      </div>

      {error && (
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
          <button onClick={() => setError(null)}>&times;</button>
        </div>
      )}

      <div className="filters-bar">
        <div className="filters-row">
          <div className="filter-group">
            <label>Start Date</label>
            <input
              type="date"
              value={filters.start_date}
              onChange={(e) => handleFilterChange('start_date', e.target.value)}
            />
          </div>
          <div className="filter-group">
            <label>End Date</label>
            <input
              type="date"
              value={filters.end_date}
              onChange={(e) => handleFilterChange('end_date', e.target.value)}
            />
          </div>
          <div className="filter-group">
            <label>Provider</label>
            <select
              value={filters.provider}
              onChange={(e) => handleFilterChange('provider', e.target.value)}
            >
              <option value="">All Providers</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="ollama">Ollama</option>
            </select>
          </div>
          <div className="filter-group">
            <label>&nbsp;</label>
            <button className="btn-primary" onClick={applyFilters}>
              Apply Filters
            </button>
          </div>
        </div>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon blue">📊</div>
            <span className="stat-label">Total Requests</span>
          </div>
          <div className="stat-value">{stats.total_requests?.toLocaleString() || 0}</div>
          <div className="stat-change positive">
            +{calculateChange(stats.total_requests, stats.previous_requests || 0)}% from last period
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon green">🔤</div>
            <span className="stat-label">Total Tokens</span>
          </div>
          <div className="stat-value">
            {stats.total_tokens > 1000000
              ? `${(stats.total_tokens / 1000000).toFixed(2)}M`
              : stats.total_tokens?.toLocaleString() || 0}
          </div>
          <div className="stat-change positive">
            +{calculateChange(stats.total_tokens, stats.previous_tokens || 0)}% from last period
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon purple">💰</div>
            <span className="stat-label">Total Cost</span>
          </div>
          <div className="stat-value">${(stats.total_cost || 0).toFixed(2)}</div>
          <div className="stat-change negative">
            +{calculateChange(stats.total_cost, stats.previous_cost || 0)}% from last period
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-header">
            <div className="stat-icon orange">🔑</div>
            <span className="stat-label">Active Keys</span>
          </div>
          <div className="stat-value">{stats.active_keys || 0}</div>
          <div className="stat-subtext">Currently active</div>
        </div>
      </div>

      <div className="charts-section">
        {renderBarChart(
          usageData.by_provider,
          'provider',
          'tokens',
          'Token Usage by Provider'
        )}

        {renderBarChart(
          usageData.by_model,
          'model',
          'requests',
          'Requests by Model'
        )}

        {renderBreakdownTable(
          usageData.by_provider,
          'Cost Breakdown by Provider'
        )}

        {renderBreakdownTable(
          usageData.by_model,
          'Cost Breakdown by Model'
        )}

        {usageData.by_user && usageData.by_user.length > 0 && renderBreakdownTable(
          usageData.by_user,
          'Usage by Virtual Key'
        )}
      </div>
    </div>
  );
}

export default UsageAnalytics;
