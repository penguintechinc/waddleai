import { usePriceSuggestion } from '../../../hooks/useVllmFleet';
import ChargebackStep from '../ChargebackStep';

/**
 * Wizard step 4: thin wrapper that fetches the TCO suggestion for the chosen
 * layout and hands it to the shared ChargebackStep.
 * @param {{state:object, dispatch:Function}} props
 * @returns {JSX.Element}
 */
export default function Step4Chargeback({ state, dispatch }) {
  const l = state.layout;
  const params = l && {
    kind: 1,
    vendor: l.vendor,
    gpu_class: l.gpu_class,
    tp: l.tensor_parallel,
    pp: l.pipeline_parallel,
    replicas: state.replicasMax,
    params_b: state.paramsB,
  };
  const rpc = state.engine === 'llamacpp_rpc';
  const q = usePriceSuggestion(params, { enabled: Boolean(l) && !rpc });
  return (
    <ChargebackStep
      kind={rpc ? 3 : 1}
      priceMode={state.priceMode}
      pricesInput={state.pricesInput}
      pricesOutput={state.pricesOutput}
      comparisonModel={state.comparisonModel}
      suggestion={q.data}
      suggestionError={q.error?.message}
      onChange={(patch) => dispatch({ type: 'SET', patch })}
    />
  );
}
