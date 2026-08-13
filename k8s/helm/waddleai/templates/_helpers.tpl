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
{{- if .Values.global.imageRegistry }}
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
{{- if .Values.global.imageRegistry }}
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
