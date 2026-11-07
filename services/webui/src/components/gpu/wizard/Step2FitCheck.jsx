import { Copy } from './explain';

/**
 * Wizard step 2: the live fit-check result. Layout sentences and refusals come
 * from the API and are shown verbatim; a refusal-less "does not fit" gets the
 * fixed fallback sentence.
 * @param {{state:object, dispatch:Function}} props
 * @returns {JSX.Element}
 */
export default function Step2FitCheck({ state, dispatch }) {
  const { fit, layout } = state;
  if (fit.status === 'idle') return <p>Enter the model&apos;s parameter count in step 1 to run the fit check.</p>;
  if (fit.status === 'loading') return <p>Checking whether this model fits…</p>;
  if (fit.status === 'error') {
    return <p role="alert">{`The fit check failed: ${fit.message}`}</p>;
  }
  const result = fit.result;
  if (!result.fits) {
    return <p role="alert">{result.refusal || Copy.fitFallback}</p>;
  }
  return (
    <fieldset className="layout-choices">
      <legend>Where it will run</legend>
      {result.layouts.map((l, i) => (
        <label key={`${l.gpu_class}-${l.tensor_parallel}-${l.pipeline_parallel}-${i}`}>
          <input
            type="radio"
            name="layout"
            checked={layout === l || (layout && layout.explanation === l.explanation)}
            onChange={() => dispatch({ type: 'CHOOSE_LAYOUT', layout: l })}
          />
          <span>{l.explanation}</span>
        </label>
      ))}
    </fieldset>
  );
}
