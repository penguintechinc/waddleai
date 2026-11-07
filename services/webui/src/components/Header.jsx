import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useVllmRayEnabled } from '../hooks/useVllmFleet';
import './Header.css';

function Header() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const gpuFleetEnabled = useVllmRayEnabled();

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <header className="header">
      <div className="header-container">
        <div className="header-logo">
          <img src="/logo.png" alt="WaddleAI Logo" className="logo-image" />
          <div className="logo-text">
            <h1>WaddleAI</h1>
            <p className="tagline">AI Gateway Management</p>
          </div>
        </div>
        <nav className="header-nav">
          <Link to="/" className="nav-link">Dashboard</Link>
          <Link to="/providers" className="nav-link">Providers</Link>
          <Link to="/ollama" className="nav-link">Ollama</Link>
          <Link to="/keys" className="nav-link">Virtual Keys</Link>
          <Link to="/analytics" className="nav-link">Analytics</Link>
          <Link to="/routing" className="nav-link">Routing</Link>
          <Link to="/memory" className="nav-link">Memory</Link>
          <Link to="/hooks" className="nav-link">Agent Hooks</Link>
          <Link to="/integrations" className="nav-link">Integrations</Link>
          {gpuFleetEnabled && <Link to="/gpu-fleet" className="nav-link">GPU Fleet</Link>}
        </nav>
        <div className="header-user">
          <span className="user-name">{user?.username || 'User'}</span>
          <span className="user-role">({user?.role || 'user'})</span>
          <button onClick={handleLogout} className="logout-button">Logout</button>
        </div>
      </div>
    </header>
  );
}

export default Header;
