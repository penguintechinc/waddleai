/**
 * Plain-language copy for the deploy wizard. This is the only home of these
 * strings so the wording stays consistent and reviewable in one place.
 */
import { QUANT_LABEL } from '../../../api/vllmFleetLabels';

/** Copy keyed by the setting it explains. */
export const Explain = {
  tensorParallel:
    'Tensor parallel splits one copy of the model across several GPUs in the same machine, so a model too big for one card can run. It needs fast links between those GPUs.',
  pipelineParallel:
    'Pipeline parallel splits the model into stages on different machines, so a very large model can span nodes. It adds some response delay.',
  maxContext:
    'The longest prompt plus answer, counted in tokens, that this deployment accepts. Longer limits use more GPU memory.',
  allowMixedClass:
    'Allow GPUs of different models inside one deployment.',
  kvHeadroom:
    'About 20% of GPU memory is kept free for conversation memory so busy periods do not run out of memory.',
  quantization: QUANT_LABEL,
};

/** Fixed UI copy the specification quotes verbatim. */
export const Copy = {
  ollamaSingleNode: 'Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node.',
  mixedClassWarning:
    'Mixed GPU models in one deployment can run slower and may run out of memory on the smaller card.',
  paramsRequired:
    'Enter the parameter count in billions (for example 12 or 70). It is not guessed.',
  fitFallback: 'The fit check did not explain why. Contact your administrator.',
  chargeback: 'Teams using this model are charged this virtual price against their token budget.',
};
