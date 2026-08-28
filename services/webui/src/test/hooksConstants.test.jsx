import { describe, it, expect } from 'vitest';
import { scopeLabel, isDangerousRule } from '../components/hooksConstants';

describe('hooksConstants', () => {
  describe('scopeLabel', () => {
    it('labels a global scope', () => {
      expect(scopeLabel('global', null)).toBe('Global (all organizations)');
    });

    it('labels an org scope with its ref', () => {
      expect(scopeLabel('org', '7')).toBe('Organization #7');
    });
  });

  describe('isDangerousRule', () => {
    it('is true only for an enabled deny rule', () => {
      expect(isDangerousRule('deny', true)).toBe(true);
    });

    it('is false for a disabled deny rule', () => {
      expect(isDangerousRule('deny', false)).toBe(false);
    });

    it('is false for an enabled allow rule', () => {
      expect(isDangerousRule('allow', true)).toBe(false);
    });

    it('is false for an enabled ask rule', () => {
      expect(isDangerousRule('ask', true)).toBe(false);
    });
  });
});
