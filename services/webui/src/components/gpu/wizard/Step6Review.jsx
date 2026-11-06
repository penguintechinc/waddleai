import { VENDOR_LABEL, QUANT_LABEL } from '../../../api/vllmFleetLabels';
import { deploymentLayoutLabel } from '../layoutLabel';

/**
 * Wizard step 6: read-only review of what will be deployed.
 * @param {{state:object}} props
 * @returns {JSX.Element}
 */
export default function Step6Review({ state }) {
  const l = state.layout;
  return (
    <dl className="wizard-review">
      <dt>Model</dt>
      <dd>{state.modelRef}</dd>
      <dt>Name</dt>
      <dd>{state.name}</dd>
      <dt>Where it runs</dt>
      <dd>{l ? `${VENDOR_LABEL[l.vendor] ?? l.vendor} · ${l.gpu_class} · ${deploymentLayoutLabel(l)}` : '—'}</dd>
      <dt>Replicas</dt>
      <dd>{`${state.replicasMin} to ${state.replicasMax}`}</dd>
      <dt>Compression</dt>
      <dd>{QUANT_LABEL[state.quantization]}</dd>
      <dt>Price</dt>
      <dd>{state.priceMode === 2 ? `Manual: ${state.pricesInput} in / ${state.pricesOutput} out per 1M tokens` : 'Automatic (amortized cost)'}</dd>
      <dt>Compared against</dt>
      <dd>{state.comparisonModel}</dd>
      <dt>Via proxy</dt>
      <dd>{state.exposeViaProxy ? 'Yes' : 'No'}</dd>
    </dl>
  );
}
