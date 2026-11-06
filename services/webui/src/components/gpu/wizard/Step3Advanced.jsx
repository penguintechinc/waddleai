import { Explain, Copy } from './explain';
import { QUANT_LABEL } from '../../../api/vllmFleetLabels';

const DTYPES = ['bfloat16', 'float16', 'float32'];

/**
 * Wizard step 3: advanced settings with a plain-language explanation beside
 * each. Quantization options are limited to what the fit response allows.
 * @param {{state:object, dispatch:Function}} props
 * @returns {JSX.Element}
 */
export default function Step3Advanced({ state, dispatch }) {
  const set = (patch) => dispatch({ type: 'SET', patch });
  const allowed = state.fit.result?.allowed_quantizations ?? [0];
  const layout = state.layout;
  const singleNode = state.engine === 'ollama';
  return (
    <div className="wizard-step">
      <label>
        Data type
        <select value={state.dtype} onChange={(e) => set({ dtype: e.target.value })}>
          {DTYPES.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>
      <label>
        Compression (quantization)
        <select
          value={state.quantization}
          onChange={(e) => set({ quantization: Number(e.target.value) })}
        >
          {allowed.map((q) => (
            <option key={q} value={q}>
              {QUANT_LABEL[q] ?? `Unknown quantization (${q})`}
            </option>
          ))}
        </select>
      </label>
      <label>
        Maximum context (tokens)
        <input
          type="number"
          min="1"
          value={state.maxModelLen}
          onChange={(e) => set({ maxModelLen: Number(e.target.value) })}
        />
      </label>
      <p className="explain">{Explain.maxContext}</p>
      <p className="explain">{Explain.kvHeadroom}</p>
      {layout && (
        <dl className="layout-readout">
          <dt>GPUs per replica, split inside a machine</dt>
          <dd>{layout.tensor_parallel}</dd>
          <dd className="explain">{Explain.tensorParallel}</dd>
          <dt>Machines per replica</dt>
          <dd>{singleNode ? 1 : layout.pipeline_parallel}</dd>
          <dd className="explain">{Explain.pipelineParallel}</dd>
        </dl>
      )}
      <label>
        Minimum replicas
        <input
          type="number"
          min="0"
          value={state.replicasMin}
          onChange={(e) => set({ replicasMin: Number(e.target.value) })}
        />
      </label>
      <label>
        Maximum replicas
        <input
          type="number"
          min="1"
          value={state.replicasMax}
          onChange={(e) => set({ replicasMax: Number(e.target.value) })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={state.allowMixedClass}
          onChange={(e) => set({ allowMixedClass: e.target.checked })}
        />
        Allow mixed GPU models
      </label>
      <p className="explain">{Explain.allowMixedClass}</p>
      {state.allowMixedClass && <p role="note">{Copy.mixedClassWarning}</p>}
    </div>
  );
}
