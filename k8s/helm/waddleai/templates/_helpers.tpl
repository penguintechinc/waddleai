{{/*
Expand the name of the chart.
*/}}
{{- define "waddleai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "waddleai.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "waddleai.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "waddleai.labels" -}}
helm.sh/chart: {{ include "waddleai.chart" . }}
{{ include "waddleai.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
environment: {{ .Values.environment }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "waddleai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "waddleai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "waddleai.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "waddleai.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image name for management service
*/}}
{{- define "waddleai.management.image" -}}
{{- if .Values.management.image.digest }}
{{- if .Values.global.imageRegistry }}
{{- printf "%s/%s@%s" .Values.global.imageRegistry .Values.management.image.repository .Values.management.image.digest }}
{{- else }}
{{- printf "%s@%s" .Values.management.image.repository .Values.management.image.digest }}
{{- end }}
{{- else if .Values.global.imageRegistry }}
{{- printf "%s/%s:%s" .Values.global.imageRegistry .Values.management.image.repository .Values.management.image.tag }}
{{- else }}
{{- printf "%s:%s" .Values.management.image.repository .Values.management.image.tag }}
{{- end }}
{{- end }}

{{/*
Image name for proxy service (AIProxy)
*/}}
{{- define "waddleai.proxy.image" -}}
{{- if .Values.proxy.image.digest }}
{{- if .Values.global.imageRegistry }}
{{- printf "%s/%s@%s" .Values.global.imageRegistry .Values.proxy.image.repository .Values.proxy.image.digest }}
{{- else }}
{{- printf "%s@%s" .Values.proxy.image.repository .Values.proxy.image.digest }}
{{- end }}
{{- else if .Values.global.imageRegistry }}
{{- printf "%s/%s:%s" .Values.global.imageRegistry .Values.proxy.image.repository .Values.proxy.image.tag }}
{{- else }}
{{- printf "%s:%s" .Values.proxy.image.repository .Values.proxy.image.tag }}
{{- end }}
{{- end }}

{{/*
Image name for webui service
*/}}
{{- define "waddleai.webui.image" -}}
{{- if .Values.webui.image.digest }}
{{- if .Values.global.imageRegistry }}
{{- printf "%s/%s@%s" .Values.global.imageRegistry .Values.webui.image.repository .Values.webui.image.digest }}
{{- else }}
{{- printf "%s@%s" .Values.webui.image.repository .Values.webui.image.digest }}
{{- end }}
{{- else if .Values.global.imageRegistry }}
{{- printf "%s/%s:%s" .Values.global.imageRegistry .Values.webui.image.repository .Values.webui.image.tag }}
{{- else }}
{{- printf "%s:%s" .Values.webui.image.repository .Values.webui.image.tag }}
{{- end }}
{{- end }}

{{/*
Image name for postgres
*/}}
{{- define "waddleai.postgres.image" -}}
{{- if .Values.postgres.image.digest -}}
{{- printf "%s@%s" .Values.postgres.image.repository .Values.postgres.image.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.postgres.image.repository .Values.postgres.image.tag }}
{{- end -}}
{{- end }}

{{/*
Image name for valkey
*/}}
{{- define "waddleai.valkey.image" -}}
{{- if .Values.valkey.image.digest -}}
{{- printf "%s@%s" .Values.valkey.image.repository .Values.valkey.image.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.valkey.image.repository .Values.valkey.image.tag }}
{{- end -}}
{{- end }}

{{/*
Image name for ollama
*/}}
{{- define "waddleai.ollama.image" -}}
{{- if .Values.ollama.image.digest -}}
{{- printf "%s@%s" .Values.ollama.image.repository .Values.ollama.image.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.ollama.image.repository .Values.ollama.image.tag }}
{{- end -}}
{{- end }}

{{/*
Image name for llamacpp (llama-server)
*/}}
{{- define "waddleai.llamacpp.image" -}}
{{- if .Values.llamacpp.image.digest -}}
{{- printf "%s@%s" .Values.llamacpp.image.repository .Values.llamacpp.image.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.llamacpp.image.repository .Values.llamacpp.image.tag }}
{{- end -}}
{{- end }}

{{/*
Image name for the llamacpp model-download init container
*/}}
{{- define "waddleai.llamacpp.downloaderImage" -}}
{{- if .Values.llamacpp.downloaderImage.digest -}}
{{- printf "%s@%s" .Values.llamacpp.downloaderImage.repository .Values.llamacpp.downloaderImage.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.llamacpp.downloaderImage.repository .Values.llamacpp.downloaderImage.tag }}
{{- end -}}
{{- end }}

{{/*
Sanitize an Ollama model name (e.g. "llama3:8b", "mistral/7b-instruct") into
a valid K8s resource-name segment: lowercase, ":" and "/" collapsed to "-",
truncated so "<fullname>-ollama-pull-<this>" still fits the 63-char DNS
label limit ("-ollama-pull-" plus the release fullname eats a fair chunk).
*/}}
{{- define "waddleai.ollama.modelSlug" -}}
{{- . | lower | replace ":" "-" | replace "/" "-" | replace "." "-" | replace "_" "-" | trunc 20 | trimSuffix "-" -}}
{{- end }}

{{/*
Shared pod template (spec.template) for both the Ollama DaemonSet
(ollama.mode=daemonset, default) and the pool-mode Deployment
(ollama.mode=pool) — identical pod spec either way, only the controller
kind + replica/strategy fields differ (Task 14). Consuming templates
`include` this and nindent it under their own `spec.template:` key.

Model pulls (§10.2): the `hardened` image tag ships no shell at all, so the
old `/bin/sh -c 'ollama serve & sleep 5 && ollama pull ... && kill'`
initContainer is impossible. Instead this renders a K8s 1.29+ NATIVE SIDECAR
initContainer (restartPolicy: Always) running `ollama serve`, gated by a
readinessProbe so the kubelet waits for it before starting the next
initContainer, followed by one plain initContainer per model running
`ollama pull <model>` directly against the sidecar over localhost — no
shell invoked anywhere in the pull path. This raises the chart's minimum
Kubernetes version to 1.29 wherever ollama.models is non-empty.
*/}}
{{- define "waddleai.ollama.podTemplate" -}}
metadata:
  labels:
    {{- include "waddleai.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: ollama
spec:
  {{- with .Values.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  serviceAccountName: {{ include "waddleai.serviceAccountName" . }}
  securityContext:
    fsGroup: 1000
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  {{- if .Values.ollama.models }}
  initContainers:
    - name: serve
      restartPolicy: Always # native K8s 1.29+ sidecar — stays up for the whole pod lifetime
      image: {{ include "waddleai.ollama.image" . }}
      imagePullPolicy: {{ .Values.ollama.image.pullPolicy }}
      args: ["serve"]
      env:
        - name: OLLAMA_MODELS
          value: /models
        - name: HOME
          value: /tmp
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        seccompProfile:
          type: RuntimeDefault
        capabilities:
          drop:
            - ALL
      readinessProbe: # gates the kubelet from starting the pull-* initContainers below until serve is actually accepting connections
        exec:
          command: ["/usr/bin/ollama", "list"]
        initialDelaySeconds: 2
        periodSeconds: 3
        timeoutSeconds: 5
        failureThreshold: 20
      {{- if .Values.ollama.gpu.enabled }}
      resources:
        limits:
          {{ .Values.ollama.gpu.type | default "nvidia" }}.com/gpu: {{ .Values.ollama.gpu.count | default 1 }}
      {{- end }}
      volumeMounts:
        - name: ollama-models
          mountPath: /models
        - name: tmp
          mountPath: /tmp
    {{- range .Values.ollama.models }}
    - name: pull-{{ include "waddleai.ollama.modelSlug" . }}
      image: {{ include "waddleai.ollama.image" $ }}
      imagePullPolicy: {{ $.Values.ollama.image.pullPolicy }}
      args: ["pull", {{ . | quote }}]
      env:
        - name: OLLAMA_HOST # explicit loopback — targets the "serve" sidecar above, same pod network namespace
          value: "127.0.0.1:11434"
        - name: OLLAMA_MODELS
          value: /models
        - name: HOME
          value: /tmp
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        seccompProfile:
          type: RuntimeDefault
        capabilities:
          drop:
            - ALL
      volumeMounts:
        - name: ollama-models
          mountPath: /models
        - name: tmp
          mountPath: /tmp
    {{- end }}
  {{- end }}
  containers:
    - name: ollama
      image: {{ include "waddleai.ollama.image" . }}
      imagePullPolicy: {{ .Values.ollama.image.pullPolicy }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        seccompProfile:
          type: RuntimeDefault
        capabilities:
          drop:
            - ALL
      ports:
        - name: http
          containerPort: {{ .Values.ollama.service.targetPort }}
          protocol: TCP
      env:
        - name: OLLAMA_MODELS
          value: /models
        - name: HOME
          value: /tmp
        {{- range $key, $value := .Values.ollama.env }}
        - name: {{ $key }}
          value: {{ $value | quote }}
        {{- end }}
      {{- if .Values.ollama.livenessProbe.enabled }}
      livenessProbe:
        httpGet:
          path: {{ .Values.ollama.livenessProbe.httpGet.path }}
          port: {{ .Values.ollama.livenessProbe.httpGet.port }}
        initialDelaySeconds: {{ .Values.ollama.livenessProbe.initialDelaySeconds }}
        periodSeconds: {{ .Values.ollama.livenessProbe.periodSeconds }}
        timeoutSeconds: {{ .Values.ollama.livenessProbe.timeoutSeconds }}
        failureThreshold: {{ .Values.ollama.livenessProbe.failureThreshold }}
      {{- end }}
      {{- if .Values.ollama.readinessProbe.enabled }}
      readinessProbe:
        httpGet:
          path: {{ .Values.ollama.readinessProbe.httpGet.path }}
          port: {{ .Values.ollama.readinessProbe.httpGet.port }}
        initialDelaySeconds: {{ .Values.ollama.readinessProbe.initialDelaySeconds }}
        periodSeconds: {{ .Values.ollama.readinessProbe.periodSeconds }}
        timeoutSeconds: {{ .Values.ollama.readinessProbe.timeoutSeconds }}
        failureThreshold: {{ .Values.ollama.readinessProbe.failureThreshold }}
      {{- end }}
      resources:
        {{- if .Values.ollama.gpu.enabled }}
        requests:
          {{ .Values.ollama.gpu.type | default "nvidia" }}.com/gpu: {{ .Values.ollama.gpu.count | default 1 }}
          {{- with .Values.ollama.resources.requests }}
          memory: {{ .memory }}
          cpu: {{ .cpu }}
          {{- end }}
        limits:
          {{ .Values.ollama.gpu.type | default "nvidia" }}.com/gpu: {{ .Values.ollama.gpu.count | default 1 }}
          {{- with .Values.ollama.resources.limits }}
          memory: {{ .memory }}
          cpu: {{ .cpu }}
          {{- end }}
        {{- else }}
        {{- toYaml .Values.ollama.resources | nindent 8 }}
        {{- end }}
      volumeMounts:
        - name: ollama-models
          mountPath: /models
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: ollama-models
      {{- if .Values.ollama.persistence.hostPath }}
      hostPath:
        path: {{ .Values.ollama.persistence.hostPath }}
        type: DirectoryOrCreate
      {{- else if .Values.ollama.persistence.enabled }}
      persistentVolumeClaim:
        claimName: {{ include "waddleai.fullname" . }}-ollama-models
      {{- else }}
      emptyDir: {}
      {{- end }}
    - name: tmp
      emptyDir: {}
  nodeSelector:
    {{- toYaml .Values.ollama.nodeSelector | nindent 4 }}
  {{- with .Values.ollama.tolerations }}
  tolerations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}

{{/*
Cilium reconciler topology, JSON-encoded — single source of truth shared by
the CILIUM_TOPOLOGY env var (consumed by services/management/app/services/
cilium_policy.py) and the bootstrap CiliumNetworkPolicy template, so the
Python renderer and the day-0 bootstrap policy can never drift apart.
Shape must match services/management/app/services/cilium_policy.py::DEFAULT_TOPOLOGY.
*/}}
{{- define "waddleai.cilium.topology" -}}
{{- $t := dict
      "namespace" .Values.namespace
      "gateway_name" .Values.cilium.topology.gatewayName
      "gateway_namespace" .Values.cilium.topology.gatewayNamespace
      "aiproxy_port" .Values.cilium.topology.aiproxyPort
      "postgres_port" .Values.cilium.topology.postgresPort
      "valkey_port" .Values.cilium.topology.valkeyPort
      "fleet_ports" .Values.cilium.topology.fleetPorts
      "fleet_component_key" .Values.cilium.topology.fleetComponentKey
      "fleet_components" .Values.cilium.topology.fleetComponents
      "selectors" .Values.cilium.topology.selectors
-}}
{{- $t | toJson }}
{{- end }}

{{/*
Stable, auto-generated pre-shared bearer for the proxy's internal gRPC
server (GrpcAuthInterceptor in proxy/apps/proxy_server/grpc_server.py,
wired via PROXY_GRPC_AUTH_TOKEN in proxy/apps/proxy_server/main.py).
Unlike the other secrets.* values in values.yaml (operator-set human
credentials with an obvious "changeme" default an install checklist
catches), nothing prompts anyone to set this one — it's pure
machine-to-machine, and it's the *only* check on that gRPC surface. A
static "changeme" default here would mean every fresh install shares the
same known bearer, so this key alone uses generate-once-and-keep-stable
instead of the chart's normal static-placeholder convention:
  1. Explicit override (secrets.proxyGrpcAuthToken) always wins — this is
     how beta/prod pin a value pushed by External Secrets Operator.
  2. Otherwise, reuse whatever value already lives in the cluster's
     waddleai-secrets Secret (`lookup`) so `helm upgrade` never rotates it
     out from under the proxy and drops every caller into
     UNAUTHENTICATED. `lookup` always returns empty under `helm
     template`/`--dry-run` (no live API server), so the branch below
     falls through to (3) for template rendering and CI — never breaks
     `helm template`.
  3. Otherwise (first install, or template-only rendering), generate a
     fresh 64-char random value.
*/}}
{{- define "waddleai.proxyGrpcAuthToken" -}}
{{- if .Values.secrets.proxyGrpcAuthToken -}}
{{- .Values.secrets.proxyGrpcAuthToken -}}
{{- else -}}
{{- $existing := lookup "v1" "Secret" .Values.namespace "waddleai-secrets" -}}
{{- if and $existing (hasKey $existing.data "proxy-grpc-auth-token") -}}
{{- index $existing.data "proxy-grpc-auth-token" | b64dec -}}
{{- else -}}
{{- randAlphaNum 64 -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Credential encryption key (management only — CREDENTIAL_ENCRYPTION_KEY,
consumed by shared/security/credential_encryption.py to Fernet-encrypt
provider API keys before they hit the DB). Same lookup-based
generate-once-and-keep-stable pattern as waddleai.proxyGrpcAuthToken above,
for a stronger reason: this key is NOT just an auth bearer that can be
rotated by bouncing callers — rotating it makes every already-encrypted
`enc:...` credential in the database permanently undecryptable (the
decrypt path in credential_encryption.py has no key-versioning/migration
support). So:
  1. Explicit override (secrets.credentialEncryptionKey) always wins.
  2. Otherwise, reuse whatever value already lives in the cluster's
     waddleai-secrets Secret (`lookup`), so a routine `helm upgrade` never
     silently rotates it out from under already-stored credentials.
  3. Otherwise (first install, or template-only rendering, where `lookup`
     always returns empty), generate a fresh 64-char random value.
*/}}
{{- define "waddleai.credentialEncryptionKey" -}}
{{- if .Values.secrets.credentialEncryptionKey -}}
{{- .Values.secrets.credentialEncryptionKey -}}
{{- else -}}
{{- $existing := lookup "v1" "Secret" .Values.namespace "waddleai-secrets" -}}
{{- if and $existing (hasKey $existing.data "credential-encryption-key") -}}
{{- index $existing.data "credential-encryption-key" | b64dec -}}
{{- else -}}
{{- randAlphaNum 64 -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
This deployment's public hostname, resolved from whichever ingress
mechanism is active. httproute.host wins when the Gateway API HTTPRoute
path is enabled (beta/prod); otherwise the first ingress.hosts[].host
when the classic Ingress path is enabled (alpha). Neither may be set for
a bare `helm template`/dry run with both disabled, hence the possible
empty result -- consumers only wire it in when non-empty (see `with`
usage at each call site). Feeds WADDLEAI_PUBLIC_HOST (management, proxy
Deployments), the sole input to penguin_licensing's domain-based licence
bypass (shared/licensing/domain_bypass.py) -- never invented
independently of the host this chart already routes external traffic to.
*/}}
{{- define "waddleai.publicHost" -}}
{{- if .Values.httproute.host -}}
{{- .Values.httproute.host -}}
{{- else if and .Values.ingress.enabled .Values.ingress.hosts -}}
{{- (first .Values.ingress.hosts).host -}}
{{- end -}}
{{- end }}

{{/*
Shared DATABASE_URL secretKeyRef snippet — used by both the management
Deployment (via management.secretEnv, rendered generically below) and the
Alembic migration Job (templates/migration-job.yaml), so the Job reads the
DB connection string through the exact same waddleai-secrets/database-url
key the app itself uses, and the two can never drift apart. The migration
Job intentionally does NOT reuse the full management.env/secretEnv lists
(CACHE_HOST, CILIUM_TOPOLOGY, OLLAMA_HOST, ...) — `alembic upgrade head`
(services/management/alembic/env.py) only ever reads DATABASE_URL, and
pulling in CILIUM_TOPOLOGY would add a second hook-ordering dependency
(the cilium-topology ConfigMap) for zero benefit.
*/}}
{{- define "waddleai.management.databaseUrlEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: waddleai-secrets
      key: database-url
{{- end }}
