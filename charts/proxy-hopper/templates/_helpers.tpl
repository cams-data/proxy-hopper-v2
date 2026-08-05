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
Name of the bundled Redis StatefulSet/Service.
*/}}
{{- define "proxy-hopper.redisFullname" -}}
{{- printf "%s-redis" (include "proxy-hopper.fullname" .) }}
{{- end }}

{{/*
Redis URL — uses the bundled Redis service if redis.enabled, otherwise
backend.redis.url.
*/}}
{{- define "proxy-hopper.redisUrl" -}}
{{- if .Values.redis.enabled }}
{{- printf "redis://%s:6379/0" (include "proxy-hopper.redisFullname" .) }}
{{- else }}
{{- .Values.backend.redis.url }}
{{- end }}
{{- end }}

{{/*
Init container that blocks pod startup until Redis answers PING — avoids
CrashLoopBackOff climbing to a multi-minute wait when Redis is briefly
unavailable (e.g. still attaching its volume) at pod start. Callers are
expected to only include this when backend.type=redis.
*/}}
{{- define "proxy-hopper.waitForRedisInitContainer" -}}
- name: wait-for-redis
  image: "{{ .Values.redis.image.repository }}:{{ .Values.redis.image.tag }}"
  imagePullPolicy: {{ .Values.redis.image.pullPolicy }}
  command:
    - sh
    - -c
    - |
      until redis-cli -u "$REDIS_URL" ping; do
        echo "Waiting for Redis..."
        sleep 2
      done
  env:
    - name: REDIS_URL
      value: {{ include "proxy-hopper.redisUrl" . }}
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

{{/*
Token server in-cluster URL — use this in config.inline to keep the URL
correct regardless of release name:
  url: '{{ include "proxy-hopper.tokenServerUrl" . }}'
*/}}
{{- define "proxy-hopper.tokenServerUrl" -}}
{{- printf "http://%s-token-server:%d" (include "proxy-hopper.fullname" .) (.Values.tokenServer.port | int) }}
{{- end }}

{{/*
PROXY_HOPPER_CONFIG_STORE_URL env entry, or empty when configStore.dialect
is unset. configStore.dialect selects the mechanism:
  "sqlite"   — chart-managed local file on a PVC mounted on the main
               deployment only. Single-writer: incompatible with
               replicaCount>1 or autoscaling, and NOT wired into a
               separately-enabled admin.* Deployment (it cannot safely
               share this PVC). Use "postgres" for config shared across
               more than one pod.
  "postgres" — external, via configStore.url or configStore.existingSecret
               (sourced here from existingSecret when set, to keep
               credential-bearing URLs out of the pod spec — otherwise a
               plain configStore.url value). Works with any replica count.
*/}}
{{- define "proxy-hopper.configStoreUrlEnv" -}}
{{- if eq .Values.configStore.dialect "postgres" }}
{{- if .Values.configStore.existingSecret }}
- name: PROXY_HOPPER_CONFIG_STORE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.configStore.existingSecret }}
      key: {{ .Values.configStore.existingSecretKey | default "url" }}
{{- else }}
- name: PROXY_HOPPER_CONFIG_STORE_URL
  value: {{ .Values.configStore.url | quote }}
{{- end }}
{{- else if eq .Values.configStore.dialect "sqlite" }}
- name: PROXY_HOPPER_CONFIG_STORE_URL
  value: {{ include "proxy-hopper.configStoreSqliteUrl" . | quote }}
{{- end }}
{{- end }}

{{/*
Chart-managed local SQLite path — must match the volumeMount path used
everywhere this is mounted (deployment.yaml, config-store-pvc.yaml).
*/}}
{{- define "proxy-hopper.configStoreSqliteMountPath" -}}
/var/lib/proxy-hopper
{{- end }}

{{/*
Four slashes total after the scheme — SQLAlchemy's sqlite convention for an
absolute path (three would mean relative-to-cwd). configStoreSqliteMountPath
already contributes its own leading slash, so only /// are written here.
*/}}
{{- define "proxy-hopper.configStoreSqliteUrl" -}}
sqlite+aiosqlite:///{{ include "proxy-hopper.configStoreSqliteMountPath" . }}/config.db
{{- end }}

{{/*
Init container that applies ConfigStore migrations before the main
container starts — sqlite only. Postgres uses a pre-install/pre-upgrade
Helm hook Job instead (config-store-migrate-job.yaml); sqlite uses an
initContainer on the same pod (same structural pattern as
waitForRedisInitContainer above) since the file it migrates is on that
pod's own PVC, not a separately-reachable server.
*/}}
{{- define "proxy-hopper.configStoreMigrateInitContainer" -}}
- name: config-store-migrate
  image: {{ include "proxy-hopper.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["proxy-hopper", "migrate"]
  env:
    {{- include "proxy-hopper.configStoreUrlEnv" . | trim | nindent 4 }}
  volumeMounts:
    - name: config-store-data
      mountPath: {{ include "proxy-hopper.configStoreSqliteMountPath" . }}
{{- end }}
