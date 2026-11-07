/**
 * Wizard step 5: proxy exposure and the optional prefer-this-model rule.
 * "Prefer" is off by default and its fallback fields stay hidden until checked.
 * @param {{state:object, dispatch:Function}} props
 * @returns {JSX.Element}
 */
export default function Step5Routing({ state, dispatch }) {
  const set = (patch) => dispatch({ type: 'SET', patch });
  const prefer = state.preferLocal;
  if (state.engine === 'llamacpp_rpc') {
    return (
      <p className="wizard-step">
        llama.cpp deployments are made available through the proxy automatically, with their own key.
      </p>
    );
  }
  return (
    <div className="wizard-step">
      <label>
        <input
          type="checkbox"
          checked={state.exposeViaProxy}
          onChange={(e) => set({ exposeViaProxy: e.target.checked })}
        />
        Make available via proxy
      </label>
      <label>
        <input
          type="checkbox"
          checked={Boolean(prefer)}
          onChange={(e) =>
            set({ preferLocal: e.target.checked ? { match: { model_prefix: '' }, fallbackModel: '' } : null })
          }
        />
        Prefer this model for…
      </label>
      {prefer && (
        <div className="prefer-local">
          <label>
            Requests whose model name starts with
            <input
              type="text"
              value={prefer.match.model_prefix}
              onChange={(e) =>
                set({ preferLocal: { ...prefer, match: { model_prefix: e.target.value } } })
              }
            />
          </label>
          <label>
            Commercial fallback model when this one is unavailable
            <input
              type="text"
              value={prefer.fallbackModel}
              onChange={(e) => set({ preferLocal: { ...prefer, fallbackModel: e.target.value } })}
            />
          </label>
        </div>
      )}
    </div>
  );
}
