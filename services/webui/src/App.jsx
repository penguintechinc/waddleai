import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './contexts/AuthContext';
import { ConnectivityProvider } from './contexts/ConnectivityContext';
import ProtectedRoute from './components/ProtectedRoute';
import ConnectivityBanner from './components/ConnectivityBanner';
import ConnectivityIndicator from './components/ConnectivityIndicator';
import Header from './components/Header';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Providers from './pages/Providers';
import OllamaDeployments from './pages/OllamaDeployments';
import VirtualKeys from './pages/VirtualKeys';
import UsageAnalytics from './pages/UsageAnalytics';
import Routing from './pages/Routing';
import Memory from './pages/Memory';
import Hooks from './pages/Hooks';
import Integrations from './pages/Integrations';
import './App.css';

function App() {
  return (
    <Router>
      <ConnectivityProvider>
        <AuthProvider>
          <ConnectivityBanner />
          <div className="connectivity-status-bar">
            <ConnectivityIndicator />
          </div>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route
              path="/*"
              element={
                <ProtectedRoute>
                  <div className="app">
                    <Header />
                    <main className="main-content">
                      <Routes>
                        <Route path="/" element={<Dashboard />} />
                        <Route path="/providers" element={<Providers />} />
                        <Route path="/ollama" element={<OllamaDeployments />} />
                        <Route path="/keys" element={<VirtualKeys />} />
                        <Route path="/analytics" element={<UsageAnalytics />} />
                        <Route path="/routing" element={<Routing />} />
                        <Route path="/memory" element={<Memory />} />
                        <Route path="/hooks" element={<Hooks />} />
                        <Route path="/integrations" element={<Integrations />} />
                        <Route path="*" element={<Navigate to="/" replace />} />
                      </Routes>
                    </main>
                  </div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </AuthProvider>
      </ConnectivityProvider>
    </Router>
  );
}

export default App;
