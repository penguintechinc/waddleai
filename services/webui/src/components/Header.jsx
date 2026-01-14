import { Link } from 'react-router-dom';
import './Header.css';

function Header() {
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
        </nav>
        <div className="header-user">
          <span className="user-name">Admin</span>
        </div>
      </div>
    </header>
  );
}

export default Header;
