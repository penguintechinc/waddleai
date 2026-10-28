import { describe, it, expect } from 'vitest';
import { hostname } from './network.js';

// regression: node_libs hostname regex was corrupted (empty char classes) + unbuildable
describe('hostname validator', () => {
  const v = hostname();
  const label63 = 'a'.repeat(63);

  it.each([
    'example.com',
    'a.b.c',
    'xn--d1acufc.example',
    `${label63}.com`,
    'ExAmPlE.CoM',
    'localhost',
  ])('accepts %s', (h) => {
    expect(v(h).isValid).toBe(true);
  });

  it.each([
    ['leading hyphen', '-foo.com'],
    ['trailing hyphen', 'foo-.com'],
    ['hyphen after dot', 'foo.-bar.com'],
    ['empty label', 'a..b'],
    ['label > 63', `${'a'.repeat(64)}.com`],
    ['total > 253', `${'a'.repeat(60)}.`.repeat(5) + 'com'],
    ['space', 'foo .com'],
    ['scheme', 'http://x'],
    ['trailing dot', 'example.com.'],
    ['leading dot', '.example.com'],
    ['empty', ''],
    ['underscore', 'foo_bar.com'],
  ])('rejects %s', (_n, h) => {
    expect(v(h).isValid).toBe(false);
  });

  it('rejects non-string', () => {
    expect(v(5 as unknown as string).isValid).toBe(false);
  });

  it('requireTld=false allows single label; true requires a dot', () => {
    expect(hostname({ requireTld: false })('localhost').isValid).toBe(true);
    expect(hostname({ requireTld: true })('localhost').isValid).toBe(false);
    expect(hostname({ requireTld: true })('example.com').isValid).toBe(true);
  });

  it('allowIp accepts IPs only when enabled', () => {
    expect(hostname({ allowIp: true })('10.0.0.1').isValid).toBe(true);
    expect(hostname({ allowIp: true })('::1').isValid).toBe(true);
    expect(hostname()('::1').isValid).toBe(false);
  });
});
