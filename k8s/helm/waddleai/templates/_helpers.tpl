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
{{- printf "%s:%s" .Values.postgres.image.repository .Values.postgres.image.tag }}
{{- end }}

{{/*
Image name for redis
*/}}
{{- define "waddleai.redis.image" -}}
{{- printf "%s:%s" .Values.redis.image.repository .Values.redis.image.tag }}
{{- end }}
