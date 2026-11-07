import { useEffect, useReducer } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { fitCheck } from '../api/vllmFleet';
import { useCreateDeployment, useVllmBackend, useVllmRayEnabled } from '../hooks/useVllmFleet';
import {
  STEP_TITLES,
  initialWizardState,
  wizardReducer,
  validateStep,
  allStepsValid,
  buildCreateBody,
  buildFitBody,
} from '../components/gpu/wizard/wizardState';
import Step1PickModel from '../components/gpu/wizard/Step1PickModel';
import Step2FitCheck from '../components/gpu/wizard/Step2FitCheck';
import Step3Advanced from '../components/gpu/wizard/Step3Advanced';
import Step4Chargeback from '../components/gpu/wizard/Step4Chargeback';
import Step5Routing from '../components/gpu/wizard/Step5Routing';
import Step6Review from '../components/gpu/wizard/Step6Review';
import './GpuFleetDeployWizard.css';

const STEPS = [Step1PickModel, Step2FitCheck, Step3Advanced, Step4Chargeback, Step5Routing, Step6Review];
const FIT_DEBOUNCE_MS = 400;

/**
 * Six-step deploy wizard with a live, debounced fit check. A fit result is
 * cleared the moment any input changes, so a stale "fits" never enables Deploy.
 * Renders nothing unless both gating flags are on.
 * @returns {JSX.Element|null}
 */
export default function GpuFleetDeployWizard() {
  const enabled = useVllmRayEnabled();
  const navigate = useNavigate();
  const [state, dispatch] = useReducer(wizardReducer, undefined, initialWizardState);
  const backend = useVllmBackend();
  const create = useCreateDeployment();
  const fitKey = JSON.stringify(buildFitBody(state));

  useEffect(() => {
    const body = JSON.parse(fitKey);
    if (!body) {
      dispatch({ type: 'FIT_RESET' });
      return undefined;
    }
    dispatch({ type: 'FIT_REQUEST' });
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const result = await fitCheck(body);
        if (!cancelled) dispatch({ type: 'FIT_OK', result });
      } catch (err) {
        if (!cancelled) dispatch({ type: 'FIT_ERROR', message: err.message });
      }
    }, FIT_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [fitKey]);

  if (!enabled) return null;

  const Step = STEPS[state.step - 1];
  const current = validateStep(state.step, state);
  const vllm = state.engine === 'vllm_ray';
  const backendRow = backend.data;
  const canDeploy = vllm && Boolean(backendRow) && allStepsValid(state) && !create.isPending;

  const deploy = () =>
    create.mutate(buildCreateBody(state, backendRow.id), {
      onSuccess: () => navigate('/gpu-fleet/deployments'),
    });

  return (
    <div className="gpu-fleet-wizard">
      <h1>Deploy a model</h1>
      <ol className="wizard-stepper">
        {STEP_TITLES.map((title, i) => (
          <li key={title} aria-current={state.step === i + 1 ? 'step' : undefined}>
            {`${i + 1}. ${title}`}
          </li>
        ))}
      </ol>
      <Step state={state} dispatch={dispatch} />
      {state.engine === 'ollama' && (
        <p>
          Ollama deployments are created on the <Link to="/ollama">Ollama page</Link>, which includes the same
          chargeback step.
        </p>
      )}
      {state.engine === 'llamacpp' && (
        <p>llama.cpp deployments are created through the llama.cpp deployments API for now.</p>
      )}
      {!current.ok && (
        <ul className="wizard-errors" role="status">
          {current.errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {create.error && <p role="alert">{create.error.message}</p>}
      {backend.isSuccess && !backendRow && (
        <p role="alert">No vLLM backend is registered. Register one from the Fleet backends API.</p>
      )}
      <div className="wizard-nav">
        <button
          type="button"
          disabled={state.step === 1}
          onClick={() => dispatch({ type: 'GOTO', step: state.step - 1 })}
        >
          Back
        </button>
        {state.step < 6 ? (
          <button
            type="button"
            disabled={!current.ok}
            onClick={() => dispatch({ type: 'GOTO', step: state.step + 1 })}
          >
            Next
          </button>
        ) : (
          <button type="button" className="btn-primary" disabled={!canDeploy} onClick={deploy}>
            Deploy
          </button>
        )}
      </div>
    </div>
  );
}
