import { useState } from 'react';
import { useModelSearch } from '../../../hooks/useVllmFleet';
import { Copy } from './explain';

const ENGINES = [
  { value: 'vllm_ray', label: 'vLLM on Ray (recommended, can span several nodes)' },
  { value: 'llamacpp', label: 'llama.cpp (set up from the llama.cpp deployments API)' },
  { value: 'ollama', label: 'Ollama (single node)' },
];

/**
 * Wizard step 1: engine, then model. Registry models come from the search API;
 * a HuggingFace repo id can be typed instead. The parameter count is required
 * whenever the registry does not know it.
 * @param {{state:object, dispatch:Function}} props
 * @returns {JSX.Element}
 */
export default function Step1PickModel({ state, dispatch }) {
  const [q, setQ] = useState('');
  const search = useModelSearch({ q, limit: 10 });
  const set = (patch) => dispatch({ type: 'SET', patch });
  const slug = (ref) => ref.split('/').pop().toLowerCase().replace(/[^a-z0-9-]+/g, '-');

  const pick = (m) =>
    set({
      modelRef: m.name,
      modelName: m.name,
      name: slug(m.name),
      paramsB: m.params_b ?? null,
    });

  return (
    <div className="wizard-step">
      <fieldset>
        <legend>Engine</legend>
        {ENGINES.map((e) => (
          <label key={e.value}>
            <input
              type="radio"
              name="engine"
              value={e.value}
              checked={state.engine === e.value}
              onChange={() => set({ engine: e.value })}
            />
            {e.label}
          </label>
        ))}
        {state.engine === 'ollama' && <p>{Copy.ollamaSingleNode}</p>}
      </fieldset>
      <fieldset>
        <legend>Model source</legend>
        {[['registry', 'Model registry'], ['hf', 'HuggingFace repo id']].map(([v, l]) => (
          <label key={v}>
            <input
              type="radio"
              name="source"
              checked={state.modelSource === v}
              onChange={() => set({ modelSource: v })}
            />
            {l}
          </label>
        ))}
      </fieldset>
      {state.modelSource === 'registry' ? (
        <div>
          <label>
            Search models
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <ul className="model-results">
            {(search.data ?? []).map((m) => (
              <li key={m.public_id}>
                <button
                  type="button"
                  aria-pressed={state.modelRef === m.name}
                  onClick={() => pick(m)}
                >
                  {m.name}
                </button>
              </li>
            ))}
          </ul>
          {search.error && <p role="alert">{search.error.message}</p>}
        </div>
      ) : (
        <label>
          HuggingFace repo id
          <input
            type="text"
            value={state.modelRef}
            placeholder="org/model-name"
            onChange={(e) =>
              set({ modelRef: e.target.value, modelName: e.target.value, name: slug(e.target.value) })
            }
          />
        </label>
      )}
      <label>
        Parameters (billions)
        <input
          type="number"
          min="0"
          step="any"
          required
          value={state.paramsB ?? ''}
          onChange={(e) => set({ paramsB: e.target.value === '' ? null : Number(e.target.value) })}
        />
      </label>
      <label>
        Deployment name
        <input type="text" value={state.name} onChange={(e) => set({ name: e.target.value })} />
      </label>
    </div>
  );
}
