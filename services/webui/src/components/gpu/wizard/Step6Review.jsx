import { VENDOR_LABEL, QUANT_LABEL } from '../../../api/vllmFleetLabels';
import { bytesToGb } from './engineCopy';
import { deploymentLayoutLabel } from '../layoutLabel';

/**
 * Wizard step 6: read-only review of what will be deployed.
 * @param {{state:object}} props
 * @returns {JSX.Element}
 */
export default function Step6Review({ state }) {
  const l = state.layout;
  if (state.engine === 'llamacpp_rpc') {
    return (
      <dl className="wizard-review">
        <dt>Engine</dt>
        <dd>llama.cpp RPC (multi-node)</dd>
        <dt>Model</dt>
        <dd>{state.modelName}</dd>
        <dt>Name</dt>
        <dd>{state.name}</dd>
        <dt>GGUF file</dt>
        <dd>{`${state.modelFilename} · ${bytesToGb(state.ggufBytes)} GB`}</dd>
        <dt>Maximum context</dt>
        <dd>{`${state.maxModelLen} tokens`}</dd>
        <dt>Mixed GPU models</dt>
        <dd>{state.allowMixedClass ? 'Allowed' : 'Not allowed'}</dd>
        <dt>Price</dt>
        <dd>{state.priceMode === 2 ? `Manual: ${state.pricesInput} in / ${state.pricesOutput} out per 1M tokens` : 'Automatic (amortized cost)'}</dd>
        <dt>Compared against</dt>
        <dd>{state.comparisonModel}</dd>
      </dl>
    );
  }
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
