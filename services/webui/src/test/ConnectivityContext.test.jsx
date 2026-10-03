import { render, screen, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ConnectivityProvider, useConnectivity } from '../contexts/ConnectivityContext';

function Probe() {
  const { isOnline, backendReachable, connected, reportSuccess, reportFailure } = useConnectivity();
  return (
    <div>
      <div data-testid="isOnline">{String(isOnline)}</div>
      <div data-testid="backendReachable">{String(backendReachable)}</div>
      <div data-testid="connected">{String(connected)}</div>
      <button data-testid="report-success" onClick={reportSuccess}>ok</button>
      <button data-testid="report-failure" onClick={reportFailure}>fail</button>
    </div>
  );
}

describe('ConnectivityContext', () => {
  let originalOnLine;

  beforeEach(() => {
    originalOnLine = window.navigator.onLine;
  });

  afterEach(() => {
    Object.defineProperty(window.navigator, 'onLine', { value: originalOnLine, configurable: true });
  });

  it('throws when useConnectivity is used outside the provider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    function Bad() {
      useConnectivity();
      return null;
    }
    expect(() => render(<Bad />)).toThrow('useConnectivity must be used within ConnectivityProvider');
    consoleError.mockRestore();
  });

  it('starts optimistic: online + backend reachable + connected', () => {
    render(
      <ConnectivityProvider>
        <Probe />
      </ConnectivityProvider>
    );

    expect(screen.getByTestId('isOnline')).toHaveTextContent('true');
    expect(screen.getByTestId('backendReachable')).toHaveTextContent('true');
    expect(screen.getByTestId('connected')).toHaveTextContent('true');
  });

  it('reportFailure flips backendReachable and connected to false; reportSuccess restores it', () => {
    render(
      <ConnectivityProvider>
        <Probe />
      </ConnectivityProvider>
    );

    act(() => {
      screen.getByTestId('report-failure').click();
    });
    expect(screen.getByTestId('backendReachable')).toHaveTextContent('false');
    expect(screen.getByTestId('connected')).toHaveTextContent('false');

    act(() => {
      screen.getByTestId('report-success').click();
    });
    expect(screen.getByTestId('backendReachable')).toHaveTextContent('true');
    expect(screen.getByTestId('connected')).toHaveTextContent('true');
  });

  it('reflects window "offline" and "online" events regardless of backend state', () => {
    render(
      <ConnectivityProvider>
        <Probe />
      </ConnectivityProvider>
    );

    act(() => {
      Object.defineProperty(window.navigator, 'onLine', { value: false, configurable: true });
      window.dispatchEvent(new Event('offline'));
    });
    expect(screen.getByTestId('isOnline')).toHaveTextContent('false');
    expect(screen.getByTestId('connected')).toHaveTextContent('false');

    act(() => {
      Object.defineProperty(window.navigator, 'onLine', { value: true, configurable: true });
      window.dispatchEvent(new Event('online'));
    });
    expect(screen.getByTestId('isOnline')).toHaveTextContent('true');
    expect(screen.getByTestId('connected')).toHaveTextContent('true');
  });

  it('connected is false when online but backend unreachable, and false when offline even if backend last reported success', () => {
    render(
      <ConnectivityProvider>
        <Probe />
      </ConnectivityProvider>
    );

    act(() => {
      screen.getByTestId('report-failure').click();
    });
    expect(screen.getByTestId('connected')).toHaveTextContent('false');

    act(() => {
      screen.getByTestId('report-success').click();
      Object.defineProperty(window.navigator, 'onLine', { value: false, configurable: true });
      window.dispatchEvent(new Event('offline'));
    });
    expect(screen.getByTestId('backendReachable')).toHaveTextContent('true');
    expect(screen.getByTestId('connected')).toHaveTextContent('false');
  });
});
