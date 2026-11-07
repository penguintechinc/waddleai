import React from 'react'
import ReactDOM from 'react-dom/client'
import axios from 'axios'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './index.css'

// Cookie-based auth (audit-2026-09-14): the access token lives in an HttpOnly
// cookie the app cannot read. Two axios defaults make every page's API call
// carry it correctly, without touching each call site:
//   * withCredentials — send the same-origin auth cookie on every request
//     (and, were the API ever cross-origin, honour its credentialed CORS),
//   * X-Requested-With — the CSRF marker the backend requires on cookie-
//     authenticated state-changing requests. A cross-site attacker cannot set
//     this header without a CORS preflight the API refuses, so its presence
//     proves the request came from this same-origin SPA.
axios.defaults.withCredentials = true
axios.defaults.headers.common['X-Requested-With'] = 'XMLHttpRequest'

// One QueryClient for the whole SPA; server state for the GPU Fleet pages lives here.
const queryClient = new QueryClient()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
)
