import { useSavings } from '../../hooks/useVllmFleet';

const money = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

const KINDS = [
  { value: '', label: 'All local models' },
  { value: '1', label: 'vLLM' },
  { value: '2', label: 'Ollama' },
  { value: '3', label: 'llama.cpp' },
];

/**
 * Money from the API's exact decimal string; display formatting only.
 * @param {string|null|undefined} value
 * @returns {string}
 */
const fmt = (value) => money.format(Number(value));

/**
 * Savings card: what local models would have cost on commercial models versus
 * the virtual price charged. Every number is an API value; nothing is
 * recomputed here. Zero tokens shows an explicit empty message.
 * @param {{period:string, onPeriodChange:Function, kind?:number, onKindChange:Function}} props
 * @returns {JSX.Element}
 */
export default function SavingsCard({ period, onPeriodChange, kind, onKindChange }) {
  const { data, isLoading, error } = useSavings({ period, kind });
  const days = period === '7d' ? 7 : 30;
  let body;
  if (isLoading) {
    body = <p>Loading savings…</p>;
  } else if (error || !data) {
    body = <p role="alert">{error?.message || 'Savings are unavailable.'}</p>;
  } else if (data.tokens_input + data.tokens_output === 0) {
    body = <p>{`No tokens served locally in the last ${days} days`}</p>;
  } else {
    body = (
      <>
        <p className="savings-headline">{`${fmt(data.savings)} saved`}</p>
        <dl className="savings-detail">
          <dt>Virtual cost charged</dt>
          <dd>{fmt(data.virtual_cost)}</dd>
          <dt>Same usage on commercial models</dt>
          <dd>{fmt(data.commercial_equivalent_cost)}</dd>
        </dl>
        {data.rows_missing_price > 0 && (
          <p className="savings-note">
            Some usage had no price on record and is not counted.
          </p>
        )}
      </>
    );
  }
  return (
    <section className="savings-card" aria-label="Savings">
      <h3>Savings</h3>
      <div className="savings-controls">
        <div role="group" aria-label="Period">
          {['7d', '30d'].map((p) => (
            <button
              key={p}
              type="button"
              className={p === period ? 'seg active' : 'seg'}
              aria-pressed={p === period}
              onClick={() => onPeriodChange(p)}
            >
              {p === '7d' ? 'Last 7 days' : 'Last 30 days'}
            </button>
          ))}
        </div>
        <label>
          Model type
          <select
            value={kind ? String(kind) : ''}
            onChange={(e) => onKindChange(e.target.value ? Number(e.target.value) : undefined)}
          >
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {body}
    </section>
  );
}
