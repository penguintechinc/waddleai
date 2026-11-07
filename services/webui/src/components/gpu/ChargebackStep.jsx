import { Copy } from './wizard/explain';

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/**
 * Format a numeric API value for display only.
 * @param {number|string|undefined} v
 * @returns {string}
 */
const fmt = (v) => usd.format(Number(v));

/**
 * Shared chargeback step (wizard step 4 and the Ollama form). Default is the
 * automatic amortized-cost price with its breakdown; "Set price manually"
 * reveals the price inputs. Numbers come from the server's TCO suggestion.
 * @param {{kind:number, priceMode:number, pricesInput:string, pricesOutput:string,
 *   comparisonModel:string, suggestion?:object, suggestionError?:string,
 *   onChange:(patch:object)=>void}} props
 * @returns {JSX.Element}
 */
export default function ChargebackStep({
  kind,
  priceMode,
  pricesInput,
  pricesOutput,
  comparisonModel,
  suggestion,
  suggestionError,
  onChange,
}) {
  const manual = priceMode === 2;
  const breakdown = suggestion?.breakdown ?? {};

  const goManual = () =>
    onChange({
      priceMode: 2,
      pricesInput: pricesInput || (suggestion ? Number(suggestion.price_input_per_1m).toFixed(2) : ''),
      pricesOutput: pricesOutput || (suggestion ? Number(suggestion.price_output_per_1m).toFixed(2) : ''),
    });

  return (
    <section className="chargeback-step" aria-label="Chargeback" data-kind={kind}>
      <h3>Chargeback</h3>
      <p>{Copy.chargeback}</p>
      {suggestionError && <p role="alert">{suggestionError}</p>}
      {!manual && suggestion && (
        <div className="tco-breakdown">
          <h4>How the automatic price is worked out</h4>
          <dl>
            <dt>Card amortization (per hour)</dt>
            <dd>{fmt(breakdown.card_amortization_hourly_usd)}</dd>
            <dt>Power (per hour)</dt>
            <dd>{fmt(breakdown.gpu_power_hourly_usd)}</dd>
            <dt>Ecosystem share (servers, storage, network, per hour)</dt>
            <dd>{fmt(suggestion.node_hourly_usd)}</dd>
            <dt>Assumed speed</dt>
            <dd>{`${suggestion.tokens_per_s_used} tokens/s`}</dd>
            <dt>Speed source</dt>
            <dd>{suggestion.source === 'observed' ? 'observed' : 'estimate'}</dd>
            <dt>Suggested input price per 1M tokens</dt>
            <dd>{fmt(suggestion.price_input_per_1m)}</dd>
            <dt>Suggested output price per 1M tokens</dt>
            <dd>{fmt(suggestion.price_output_per_1m)}</dd>
          </dl>
        </div>
      )}
      {!manual && !suggestion && !suggestionError && <p>Working out the automatic price…</p>}
      {manual ? (
        <div className="manual-price">
          <label>
            Input price per 1M tokens (USD)
            <input
              type="text"
              inputMode="decimal"
              value={pricesInput}
              onChange={(e) => onChange({ pricesInput: e.target.value })}
            />
          </label>
          <label>
            Output price per 1M tokens (USD)
            <input
              type="text"
              inputMode="decimal"
              value={pricesOutput}
              onChange={(e) => onChange({ pricesOutput: e.target.value })}
            />
          </label>
          <button type="button" onClick={() => onChange({ priceMode: 1 })}>
            Use the automatic price
          </button>
        </div>
      ) : (
        <button type="button" onClick={goManual}>
          Set price manually
        </button>
      )}
      <label>
        Compare against (provider/model)
        <input
          type="text"
          value={comparisonModel}
          placeholder="openai/gpt-4o-mini"
          onChange={(e) => onChange({ comparisonModel: e.target.value })}
        />
      </label>
    </section>
  );
}
