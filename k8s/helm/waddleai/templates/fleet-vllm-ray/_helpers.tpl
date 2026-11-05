{{/*
vLLM-on-Ray fleet helpers (underscore-prefixed: never rendered as a manifest).
*/}}

{{/*
Validate fleet.vllmRay and fail loud on anything that would render a broken cluster.
Called once from raycluster.yaml. Messages name the offending values key.
*/}}
{{- define "waddleai.vllmRay.validate" -}}
{{- $v := .Values.fleet.vllmRay -}}
{{- if not $v.workerGroups -}}
{{- fail "fleet.vllmRay.workerGroups must list at least one worker group when fleet.vllmRay.enabled=true" -}}
{{- end -}}
{{- $classNames := list -}}
{{- range $v.nvidiaClassMap -}}
{{- $classNames = append $classNames (printf "nvidia-%s" .name) -}}
{{- end -}}
{{- range $i, $g := $v.workerGroups -}}
{{- if not (kindIs "map" $g) -}}
{{- fail (printf "fleet.vllmRay.workerGroups[%d] must be a mapping (got %v)" $i $g) -}}
{{- end -}}
{{- if lt (int $g.gpusPerWorker) 1 -}}
{{- fail (printf "fleet.vllmRay.workerGroups[%d] (%s): gpusPerWorker must be >= 1" $i $g.name) -}}
{{- end -}}
{{- if and (eq $g.vendor "nvidia") (not (has $g.gpuClass $classNames)) -}}
{{- fail (printf "fleet.vllmRay.workerGroups[%d] (%s): gpuClass %q is not derived from any fleet.vllmRay.nvidiaClassMap entry (expected one of %v)" $i $g.name $g.gpuClass $classNames) -}}
{{- end -}}
{{- if not (hasKey $v.images (default "" $g.image)) -}}
{{- fail (printf "fleet.vllmRay.workerGroups[%d] (%s): image %q is not a key of fleet.vllmRay.images (cuda|rocm|xpu)" $i $g.name $g.image) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Resolve one fleet.vllmRay.images entry to repository:tag@sha256:digest.
Args: dict "ctx" <root> "key" <images key>. Fails when the digest is empty (pins are mandatory),
when the tag does not carry ray.version, or when it carries a different Python minor.
*/}}
{{- define "waddleai.vllmRay.image" -}}
{{- $v := .ctx.Values.fleet.vllmRay -}}
{{- $img := index $v.images .key -}}
{{- if not $img.digest -}}
{{- fail (printf "fleet.vllmRay.images.%s.digest is empty: images must be pinned by sha256 digest" .key) -}}
{{- end -}}
{{- if not (contains $v.ray.version $img.tag) -}}
{{- fail (printf "fleet.vllmRay.images.%s.tag %q does not contain ray.version %s: head and workers must run the same Ray" .key $img.tag $v.ray.version) -}}
{{- end -}}
{{- $py := printf "py%s" (replace "." "" $v.ray.pythonMinor) -}}
{{- if not (contains $py $img.tag) -}}
{{- fail (printf "fleet.vllmRay.images.%s.tag %q does not contain %s: pythonMinor must match across images" .key $img.tag $py) -}}
{{- end -}}
{{- printf "%s:%s@%s" $img.repository $img.tag $img.digest -}}
{{- end -}}

{{/*
Pod-level securityContext shared by head and workers (rootless, PSA restricted).
*/}}
{{- define "waddleai.vllmRay.podSecurityContext" -}}
runAsUser: 1000
runAsGroup: 1000
runAsNonRoot: true
fsGroup: 1000
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/*
Container-level securityContext shared by head and workers.
*/}}
{{- define "waddleai.vllmRay.containerSecurityContext" -}}
runAsNonRoot: true
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
