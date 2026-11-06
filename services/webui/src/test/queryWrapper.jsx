import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

/**
 * Test helper: a fresh QueryClient (no retries, no cache) wrapped around the
 * rendered tree so hooks under test have a provider.
 */
export function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return function Wrapper({ children }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}
