import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Header from './components/Header';
import Dashboard from './pages/Dashboard';
import Providers from './pages/Providers';
import OllamaDeployments from './pages/OllamaDeployments';
import VirtualKeys from './pages/VirtualKeys';
import UsageAnalytics from './pages/UsageAnalytics';
import './App.css';

function App() {
  return (
    <Router>
      <div className="app">
        <Header />
        <main className="main-content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/providers" element={<Providers />} />
            <Route path="/ollama" element={<OllamaDeployments />} />
            <Route path="/keys" element={<VirtualKeys />} />
            <Route path="/analytics" element={<UsageAnalytics />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;
