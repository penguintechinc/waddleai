{{/*
Expand the name of the chart.
*/}}
{{- define "penguincode.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "penguincode.fullname" -}}
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
{{- define "penguincode.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "penguincode.labels" -}}
helm.sh/chart: {{ include "penguincode.chart" . }}
{{ include "penguincode.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "penguincode.selectorLabels" -}}
app.kubernetes.io/name: {{ include "penguincode.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "penguincode.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "penguincode.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Full image reference. Prefers an immutable SHA256 digest
(image.digest, e.g. "sha256:<digest>") over a mutable tag when both are set
-- production/gamma values files pin by digest (critical-rules.md Dependency
Pinning); alpha/beta use the tier tag pattern instead. Falls back to
image.tag, then Chart.AppVersion, matching Helm convention.
*/}}
{{- define "penguincode.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end }}

{{/*
Server labels
*/}}
{{- define "penguincode.server.labels" -}}
{{ include "penguincode.labels" . }}
app.kubernetes.io/component: server
{{- end }}

{{/*
Server selector labels
*/}}
{{- define "penguincode.server.selectorLabels" -}}
{{ include "penguincode.selectorLabels" . }}
app.kubernetes.io/component: server
{{- end }}

{{/*
Name of the Secret holding the LEAST-PRIVILEGE app-role PGVECTOR_URL -- used
by the server Deployment only. An existingSecret reference wins over the
chart-managed Secret (see templates/secret.yaml, values.yaml postgres.*).
*/}}
{{- define "penguincode.postgresSecretName" -}}
{{- default (printf "%s-secrets" (include "penguincode.fullname" .)) .Values.postgres.existingSecret -}}
{{- end }}

{{/*
Name of the Secret holding the ADMIN-privileged Postgres DSN -- used ONLY by
the role-bootstrap Job and the migration Job (CREATE ROLE / CREATE EXTENSION /
DDL all need elevated privileges). NEVER referenced by the server Deployment.
An existingSecret reference wins over the chart-managed Secret (see
templates/secret.yaml, values.yaml postgres.adminExistingSecret).
*/}}
{{- define "penguincode.postgresAdminSecretName" -}}
{{- default (printf "%s-secrets" (include "penguincode.fullname" .)) .Values.postgres.adminExistingSecret -}}
{{- end }}

{{/*
Name of the Secret holding the password used by the role-bootstrap Job's
`CREATE ROLE penguincode_app ... PASSWORD` / `ALTER ROLE ... PASSWORD`
statements -- must match the password embedded in the app-role DSN
(postgres.existingSecret). An existingSecret reference wins over the
chart-managed Secret.
*/}}
{{- define "penguincode.appRolePasswordSecretName" -}}
{{- default (printf "%s-secrets" (include "penguincode.fullname" .)) .Values.postgres.bootstrapRole.passwordExistingSecret -}}
{{- end }}

{{/*
Name of the Secret holding POSTHOG_KEY -- an existingSecret reference wins
over the chart-managed Secret (see templates/secret.yaml, values.yaml flags.posthog.*).
*/}}
{{- define "penguincode.posthogSecretName" -}}
{{- default (printf "%s-secrets" (include "penguincode.fullname" .)) .Values.flags.posthog.existingSecret -}}
{{- end }}

{{/*
Name of the Secret holding OTEL_EXPORTER_OTLP_HEADERS -- an existingSecret
reference wins over the chart-managed Secret (see templates/secret.yaml,
values.yaml otel.headers.*).
*/}}
{{- define "penguincode.otelHeadersSecretName" -}}
{{- default (printf "%s-secrets" (include "penguincode.fullname" .)) .Values.otel.headers.existingSecret -}}
{{- end }}
