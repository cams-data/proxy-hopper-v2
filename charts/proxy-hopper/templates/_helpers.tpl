{{/*
Expand the name of the chart.
*/}}
{{- define "proxy-hopper.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "proxy-hopper.fullname" -}}
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
Create chart label.
*/}}
{{- define "proxy-hopper.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "proxy-hopper.labels" -}}
helm.sh/chart: {{ include "proxy-hopper.chart" . }}
{{ include "proxy-hopper.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "proxy-hopper.selectorLabels" -}}
app.kubernetes.io/name: {{ include "proxy-hopper.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "proxy-hopper.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "proxy-hopper.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image tag — falls back to chart appVersion.
*/}}
{{- define "proxy-hopper.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Full image reference — appends -redis suffix when using Redis backend.
*/}}
{{- define "proxy-hopper.image" -}}
{{- if eq .Values.backend.type "redis" }}
{{- printf "%s:%s-redis" .Values.image.repository (include "proxy-hopper.imageTag" .) }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (include "proxy-hopper.imageTag" .) }}
{{- end }}
{{- end }}

{{/*
Redis URL — uses subchart service if redis.enabled, otherwise backend.redis.url.
*/}}
{{- define "proxy-hopper.redisUrl" -}}
{{- if .Values.redis.enabled }}
{{- printf "redis://%s-redis-master:6379/0" .Release.Name }}
{{- else }}
{{- .Values.backend.redis.url }}
{{- end }}
{{- end }}

{{/*
Name of the ConfigMap or Secret holding config.yaml.
*/}}
{{- define "proxy-hopper.configName" -}}
{{- if .Values.config.existingSecret }}
{{- .Values.config.existingSecret }}
{{- else if .Values.config.existingConfigMap }}
{{- .Values.config.existingConfigMap }}
{{- else }}
{{- include "proxy-hopper.fullname" . }}-config
{{- end }}
{{- end }}

{{/*
Whether the config volume comes from a Secret.
*/}}
{{- define "proxy-hopper.configIsSecret" -}}
{{- if .Values.config.existingSecret }}true{{- else }}false{{- end }}
{{- end }}
