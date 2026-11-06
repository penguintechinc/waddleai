{{/*
vLLM-on-Ray fleet helpers (underscore-prefixed: never rendered as a manifest).
*/}}

{{/*
Validate fleet.vllmRay and fail loud on anything that would render a broken cluster.
Called once from raycluster.yaml. Messages name the offending values key.
*/}}
{{- define "waddleai.vllmRay.validate" -}}
{{- $v := .Values.fleet.vllmRay -}}
{{- /* Network isolation is mandatory: Ray's ports are unauthenticated at the network layer and
       Serve HTTP (8000) is outside Ray TLS/token, so only the Cilium policies protect it. */ -}}
{{- if not $v.unsafeNoNetworkPolicy -}}
{{- if not (and .Values.cilium.enabled .Values.cilium.networkPolicy.enabled (.Capabilities.APIVersions.Has "cilium.io/v2")) -}}
{{- fail "fleet.vllmRay.enabled=true requires CiliumNetworkPolicies, but they will not render: need cilium.enabled=true, cilium.networkPolicy.enabled=true and the cilium.io/v2 CRD. Fix that, or set fleet.vllmRay.unsafeNoNetworkPolicy=true (UNSAFE: Ray ports left unprotected)" -}}
{{- end -}}
{{- if not .Values.fleet.security.mutualAuth -}}
{{- fail "fleet.security.mutualAuth=false leaves Ray Serve HTTP (8000) plaintext, protected only by network policy. Set mutualAuth=true, or set fleet.vllmRay.unsafeNoNetworkPolicy=true (UNSAFE) to accept it" -}}
{{- end -}}
{{- end -}}
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

{{/*
Secret name resolution. One rule for every chart-generated key (AM15):
env-sourced Secret > fleet.security.<key>.existingSecret > chart-generated.
*/}}
{{- define "waddleai.vllmRay.rayAuthSecretName" -}}
{{- $s := .Values.fleet.security.rayAuthToken -}}
{{- default (default "waddleai-ray-auth" $s.existingSecret) $s.envSecret -}}
{{- end -}}

{{- define "waddleai.vllmRay.rayTlsSecretName" -}}
{{- default "waddleai-ray-tls" .Values.fleet.security.rayTls.existingSecret -}}
{{- end -}}

{{- define "waddleai.vllmRay.tokenKeySecretName" -}}
{{- default "waddleai-fleet-token-key" .Values.fleet.security.tokenEncryptionKey.existingSecret -}}
{{- end -}}

{{/*
Ray auth + TLS container env (head and workers). Token auth is always on; the token itself is a
mounted file, never a literal. WADDLEAI_RAY_AUTH_TOKEN is added only when an env Secret is named.
*/}}
{{- define "waddleai.vllmRay.authEnv" -}}
- {name: RAY_AUTH_MODE, value: token}
- {name: RAY_AUTH_TOKEN_PATH, value: /etc/ray-auth/token}
{{- with .Values.fleet.security.rayAuthToken.envSecret }}
- name: WADDLEAI_RAY_AUTH_TOKEN
  valueFrom:
    secretKeyRef: {name: {{ . | quote }}, key: token}
{{- end }}
{{- if .Values.fleet.vllmRay.rayInternalTls }}
- {name: RAY_USE_TLS, value: "1"}
- {name: RAY_TLS_SERVER_CERT, value: /etc/ray-tls/tls.crt}
- {name: RAY_TLS_SERVER_KEY, value: /etc/ray-tls/tls.key}
- {name: RAY_TLS_CA_CERT, value: /etc/ray-tls/ca.crt}
{{- end }}
{{- end -}}

{{- define "waddleai.vllmRay.authMounts" -}}
- {name: ray-auth, mountPath: /etc/ray-auth, readOnly: true}
{{- if .Values.fleet.vllmRay.rayInternalTls }}
- {name: ray-tls, mountPath: /etc/ray-tls, readOnly: true}
{{- end }}
{{- end -}}

{{- define "waddleai.vllmRay.authVolumes" -}}
- name: ray-auth
  secret:
    secretName: {{ include "waddleai.vllmRay.rayAuthSecretName" . }}
    defaultMode: 0440
    items:
      - {key: token, path: token}
{{- if .Values.fleet.vllmRay.rayInternalTls }}
- name: ray-tls
  secret:
    secretName: {{ include "waddleai.vllmRay.rayTlsSecretName" . }}
{{- end }}
{{- end -}}
