import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Header from '../components/Header';

// Mock CSS import
vi.mock('../components/Header.css', () => ({}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
  useNavigate: () => mockNavigate,
}));

const mockLogout = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

import { useAuth } from '../contexts/AuthContext';

describe('Header', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockNavigate.mockReset();
  });

  it('renders the WaddleAI brand name', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('WaddleAI')).toBeInTheDocument();
  });

  it('renders the tagline', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('AI Gateway Management')).toBeInTheDocument();
  });

  it('renders all navigation links', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Providers')).toBeInTheDocument();
    expect(screen.getByText('Ollama')).toBeInTheDocument();
    expect(screen.getByText('Virtual Keys')).toBeInTheDocument();
    expect(screen.getByText('Analytics')).toBeInTheDocument();
    expect(screen.getByText('Routing')).toBeInTheDocument();
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('Agent Hooks')).toBeInTheDocument();
  });

  it('renders navigation links with correct hrefs', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('Dashboard').closest('a')).toHaveAttribute('href', '/');
    expect(screen.getByText('Providers').closest('a')).toHaveAttribute('href', '/providers');
    expect(screen.getByText('Ollama').closest('a')).toHaveAttribute('href', '/ollama');
    expect(screen.getByText('Virtual Keys').closest('a')).toHaveAttribute('href', '/keys');
    expect(screen.getByText('Analytics').closest('a')).toHaveAttribute('href', '/analytics');
    expect(screen.getByText('Routing').closest('a')).toHaveAttribute('href', '/routing');
    expect(screen.getByText('Memory').closest('a')).toHaveAttribute('href', '/memory');
    expect(screen.getByText('Agent Hooks').closest('a')).toHaveAttribute('href', '/hooks');
  });

  it('displays username from user context', () => {
    useAuth.mockReturnValue({ user: { username: 'alice', role: 'Viewer' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('alice')).toBeInTheDocument();
  });

  it('displays role from user context', () => {
    useAuth.mockReturnValue({ user: { username: 'alice', role: 'Viewer' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('(Viewer)')).toBeInTheDocument();
  });

  it('shows "User" as fallback when user.username is absent', () => {
    useAuth.mockReturnValue({ user: {}, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('User')).toBeInTheDocument();
  });

  it('shows "user" as fallback role when user.role is absent', () => {
    useAuth.mockReturnValue({ user: {}, logout: mockLogout });
    render(<Header />);
    expect(screen.getByText('(user)')).toBeInTheDocument();
  });

  it('renders the Logout button', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByRole('button', { name: 'Logout' })).toBeInTheDocument();
  });

  it('calls logout and navigates to /login when Logout clicked', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    expect(mockLogout).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });

  it('renders the logo image', () => {
    useAuth.mockReturnValue({ user: { username: 'admin', role: 'Admin' }, logout: mockLogout });
    render(<Header />);
    expect(screen.getByAltText('WaddleAI Logo')).toBeInTheDocument();
  });
});
