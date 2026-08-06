import { useState } from "react";
import { useQuery, useMutation, Provider } from "urql";
import { Plus, RefreshCw, Lock, Target, ShieldAlert, Wifi } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Badge } from "../../components/ui/Badge";
import { Card, CardBody, CardHeader } from "../../components/ui/Card";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { Spinner } from "../../components/ui/Spinner";
import { StatusDot } from "../../components/ui/StatusDot";
import { TARGETS_QUERY, TARGET_METRICS_QUERY, TARGET_IP_STATES_QUERY, POOLS_QUERY } from "../../graphql/queries";
import { ADD_TARGET, UPDATE_TARGET, REMOVE_TARGET } from "../../graphql/mutations";
import { getClient } from "../../lib/client";

interface ResolvedIp {
  host: string;
  port: number;
  provider: string | null;
}

interface KeyValue {
  name: string;
  value: string;
}

interface IpRuntimeState {
  address: string;
  host: string;
  port: number;
  provider: string | null;
  failures: number;
  quarantined: boolean;
  releaseAt: number | null;
  userAgent: string | null;
  requestCount: number;
  cookiesEnabled: boolean;
  profileHeaders: KeyValue[];
  cookies: KeyValue[];
  identityEnabled: boolean;
}

interface Target_ {
  name: string;
  regex: string;
  poolName: string | null;
  minRequestInterval: number;
  maxQueueWait: number;
  numRetries: number;
  ipFailuresUntilQuarantine: number;
  quarantineTime: number;
  spoofUserAgent: boolean;
  defaultProxyPort: number;
  static: boolean;
  mutable: boolean;
  resolvedIps: ResolvedIp[];
}

interface Metrics {
  name: string;
  totalRequests: number;
  successRequests: number;
  failedRequests: number;
  avgLatencyMs: number;
  lastRequestAt: string | null;
}

const EMPTY_FORM = {
  name: "",
  regex: "",
  poolName: "",
  minRequestInterval: 1,
  maxQueueWait: 30,
  numRetries: 3,
  ipFailuresUntilQuarantine: 5,
  quarantineTime: 120,
  spoofUserAgent: true,
  defaultProxyPort: 8080,
};

function TargetForm({
  initial,
  pools,
  onSave,
  onCancel,
  saving,
  error,
}: {
  initial: typeof EMPTY_FORM;
  pools: string[];
  onSave: (v: typeof EMPTY_FORM) => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
}) {
  const [form, setForm] = useState(initial);
  const setNum = (k: keyof typeof EMPTY_FORM) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: Number(e.target.value) }));

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(form);
      }}
    >
      <Input
        label="Name"
        value={form.name}
        onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
        required
        disabled={!!initial.name}
      />
      <Input
        label="URL Regex"
        value={form.regex}
        onChange={(e) => setForm((f) => ({ ...f, regex: e.target.value }))}
        placeholder="example\.com"
        required
      />
      <div className="flex flex-col gap-1">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">IP Pool</label>
        <select
          className="rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
          value={form.poolName}
          onChange={(e) => setForm((f) => ({ ...f, poolName: e.target.value }))}
        >
          <option value="">— none —</option>
          {pools.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Input
          label="Min Request Interval (s)"
          type="number"
          min={0}
          step={0.1}
          value={form.minRequestInterval}
          onChange={setNum("minRequestInterval")}
        />
        <Input
          label="Max Queue Wait (s)"
          type="number"
          min={0}
          step={1}
          value={form.maxQueueWait}
          onChange={setNum("maxQueueWait")}
        />
        <Input
          label="Num Retries"
          type="number"
          min={0}
          value={form.numRetries}
          onChange={setNum("numRetries")}
        />
        <Input
          label="Default Proxy Port"
          type="number"
          min={1}
          max={65535}
          value={form.defaultProxyPort}
          onChange={setNum("defaultProxyPort")}
        />
        <Input
          label="IP Failures Until Quarantine"
          type="number"
          min={1}
          value={form.ipFailuresUntilQuarantine}
          onChange={setNum("ipFailuresUntilQuarantine")}
        />
        <Input
          label="Quarantine Time (s)"
          type="number"
          min={0}
          value={form.quarantineTime}
          onChange={setNum("quarantineTime")}
        />
        <div className="flex items-center gap-2 pt-6">
          <input
            id="spoof-ua"
            type="checkbox"
            checked={form.spoofUserAgent}
            onChange={(e) => setForm((f) => ({ ...f, spoofUserAgent: e.target.checked }))}
            className="h-4 w-4 rounded border-gray-300 text-primary-600"
          />
          <label htmlFor="spoof-ua" className="text-sm text-gray-700 dark:text-gray-300">
            Spoof User-Agent
          </label>
        </div>
      </div>
      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2 pt-2">
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={saving}>
          {saving && <Spinner className="h-4 w-4" />}
          Save
        </Button>
      </div>
    </form>
  );
}

function MetricsPanel({ name }: { name: string }) {
  const [{ data, fetching }] = useQuery({
    query: TARGET_METRICS_QUERY,
    variables: { name },
  });
  const m: Metrics | undefined = data?.targetMetrics;

  if (fetching) return <Spinner className="h-4 w-4" />;
  if (!m) return <p className="text-sm text-gray-400">No metrics available</p>;

  const successRate =
    m.totalRequests > 0 ? ((m.successRequests / m.totalRequests) * 100).toFixed(1) : "—";

  return (
    <div className="grid grid-cols-2 gap-3 text-sm">
      <Stat label="Total Requests" value={m.totalRequests} />
      <Stat label="Success Rate" value={`${successRate}%`} />
      <Stat label="Avg Latency" value={`${m.avgLatencyMs.toFixed(0)}ms`} />
      <Stat label="Failed" value={m.failedRequests} />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
      <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
      <p className="text-lg font-semibold text-gray-900 dark:text-gray-100">{value}</p>
    </div>
  );
}

function TargetsInner() {
  const [{ data, fetching }, refetch] = useQuery({ query: TARGETS_QUERY });
  const [{ data: poolsData }] = useQuery({ query: POOLS_QUERY });
  const [, addTarget] = useMutation(ADD_TARGET);
  const [, updateTarget] = useMutation(UPDATE_TARGET);
  const [, removeTarget] = useMutation(REMOVE_TARGET);

  const [selected, setSelected] = useState<Target_ | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mutError, setMutError] = useState<string | null>(null);

  const targets: Target_[] = data?.targets ?? [];
  const poolNames: string[] = (poolsData?.pools ?? []).map((p: { name: string }) => p.name);

  async function handleAdd(form: typeof EMPTY_FORM) {
    setSaving(true);
    setMutError(null);
    const res = await addTarget({
      input: {
        name: form.name,
        regex: form.regex,
        poolName: form.poolName || null,
        minRequestInterval: form.minRequestInterval,
        maxQueueWait: form.maxQueueWait,
        numRetries: form.numRetries,
        ipFailuresUntilQuarantine: form.ipFailuresUntilQuarantine,
        quarantineTime: form.quarantineTime,
        spoofUserAgent: form.spoofUserAgent,
        defaultProxyPort: form.defaultProxyPort,
        static: false,
        mutable: true,
      },
    });
    setSaving(false);
    if (res.error) {
      setMutError(res.error.message);
    } else {
      setShowAdd(false);
      refetch({ requestPolicy: "network-only" });
    }
  }

  async function handleUpdate(form: typeof EMPTY_FORM) {
    setSaving(true);
    setMutError(null);
    const res = await updateTarget({
      input: {
        name: form.name,
        regex: form.regex,
        poolName: form.poolName || null,
        minRequestInterval: form.minRequestInterval,
        maxQueueWait: form.maxQueueWait,
        numRetries: form.numRetries,
        ipFailuresUntilQuarantine: form.ipFailuresUntilQuarantine,
        quarantineTime: form.quarantineTime,
        spoofUserAgent: form.spoofUserAgent,
        defaultProxyPort: form.defaultProxyPort,
        static: false,
        mutable: true,
      },
    });
    setSaving(false);
    if (res.error) {
      setMutError(res.error.message);
    } else {
      setShowEdit(false);
      setSelected(null);
      refetch({ requestPolicy: "network-only" });
    }
  }

  async function handleRemove(name: string) {
    if (!confirm(`Remove target "${name}"?`)) return;
    const res = await removeTarget({ name });
    if (res.error) alert(res.error.message);
    else {
      setSelected(null);
      refetch({ requestPolicy: "network-only" });
    }
  }

  return (
    <div className="flex h-full">
      <aside className="w-64 shrink-0 border-r border-gray-200 dark:border-gray-800">
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-800">
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Targets</span>
          <div className="flex gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch({ requestPolicy: "network-only" })}
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => { setShowAdd(true); setMutError(null); }}>
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {fetching && !targets.length ? (
          <div className="flex h-24 items-center justify-center">
            <Spinner />
          </div>
        ) : (
          <ul className="scrollbar-thin overflow-y-auto">
            {targets.map((t) => (
              <li key={t.name}>
                <button
                  className={`flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors ${
                    selected?.name === t.name
                      ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
                      : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  }`}
                  onClick={() => setSelected(t)}
                >
                  <Target className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                  <span className="truncate">{t.name}</span>
                  {t.static && <Lock className="ml-auto h-3 w-3 shrink-0 text-gray-400" />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <div className="flex-1 overflow-auto p-6">
        {selected ? (
          <TargetDetail
            target={selected}
            onEdit={() => { setShowEdit(true); setMutError(null); }}
            onRemove={() => handleRemove(selected.name)}
          />
        ) : (
          <div className="flex h-48 items-center justify-center text-sm text-gray-400">
            Select a target
          </div>
        )}
      </div>

      <Dialog open={showAdd} onClose={() => setShowAdd(false)} title="Add Target">
        <TargetForm
          initial={EMPTY_FORM}
          pools={poolNames}
          onSave={handleAdd}
          onCancel={() => setShowAdd(false)}
          saving={saving}
          error={mutError}
        />
      </Dialog>

      {selected && (
        <Dialog open={showEdit} onClose={() => setShowEdit(false)} title="Edit Target">
          <TargetForm
            initial={{
              name: selected.name,
              regex: selected.regex,
              poolName: selected.poolName ?? "",
              minRequestInterval: selected.minRequestInterval,
              maxQueueWait: selected.maxQueueWait,
              numRetries: selected.numRetries,
              ipFailuresUntilQuarantine: selected.ipFailuresUntilQuarantine,
              quarantineTime: selected.quarantineTime,
              spoofUserAgent: selected.spoofUserAgent,
              defaultProxyPort: selected.defaultProxyPort,
            }}
            pools={poolNames}
            onSave={handleUpdate}
            onCancel={() => setShowEdit(false)}
            saving={saving}
            error={mutError}
          />
        </Dialog>
      )}
    </div>
  );
}

function IpDetailPane({
  ip,
  threshold,
}: {
  ip: IpRuntimeState;
  threshold: number;
}) {
  const releaseInMs = ip.releaseAt ? ip.releaseAt * 1000 - Date.now() : null;
  const releaseInSec = releaseInMs !== null ? Math.max(0, Math.round(releaseInMs / 1000)) : null;

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        {ip.quarantined ? (
          <ShieldAlert className="h-4 w-4 text-red-500" />
        ) : (
          <Wifi className="h-4 w-4 text-green-500" />
        )}
        <code className="font-mono text-sm font-medium text-gray-900 dark:text-gray-100">
          {ip.address}
        </code>
      </div>

      <div className="space-y-2 text-sm">
        <DetailRow label="Status">
          {ip.quarantined ? (
            <span className="font-medium text-red-600 dark:text-red-400">Quarantined</span>
          ) : (
            <span className="font-medium text-green-600 dark:text-green-400">Active</span>
          )}
        </DetailRow>
        {ip.quarantined && releaseInSec !== null && (
          <DetailRow label="Releases in">
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {releaseInSec >= 60
                ? `${Math.floor(releaseInSec / 60)}m ${releaseInSec % 60}s`
                : `${releaseInSec}s`}
            </span>
          </DetailRow>
        )}
        <DetailRow label="Provider">
          <span className="font-medium text-gray-900 dark:text-gray-100">
            {ip.provider ?? "—"}
          </span>
        </DetailRow>
        <DetailRow label="Failures">
          <span className={`font-medium ${ip.failures > 0 ? "text-amber-600 dark:text-amber-400" : "text-gray-900 dark:text-gray-100"}`}>
            {ip.failures} / {threshold}
          </span>
        </DetailRow>
        <DetailRow label="Requests">
          <span className="font-medium text-gray-900 dark:text-gray-100">{ip.requestCount}</span>
        </DetailRow>
      </div>

      {!ip.identityEnabled ? (
        <div className="rounded border border-dashed border-gray-200 dark:border-gray-700 p-3 text-xs text-gray-400">
          Identity system not enabled for this target. Add{" "}
          <code className="font-mono">identity: enabled: true</code> to the target
          config to track per-IP browser profiles and cookies.
        </div>
      ) : (
        <>
          <div className="pt-1">
            <p className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
              Profile Headers
            </p>
            {ip.profileHeaders.length > 0 ? (
              <div className="overflow-x-auto rounded border border-gray-100 dark:border-gray-800">
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                    {ip.profileHeaders.map((h) => (
                      <tr key={h.name}>
                        <td className="w-36 shrink-0 px-2 py-1.5 font-mono text-gray-500 dark:text-gray-400">
                          {h.name}
                        </td>
                        <td className="px-2 py-1.5 font-mono text-gray-900 dark:text-gray-100 break-all">
                          {h.value}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-xs text-gray-400">No profile assigned yet.</p>
            )}
          </div>

          <div className="pt-1">
            <p className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
              Cookies {ip.cookiesEnabled ? `(${ip.cookies.length})` : "(disabled)"}
            </p>
            {ip.cookiesEnabled && ip.cookies.length > 0 ? (
              <div className="overflow-x-auto rounded border border-gray-100 dark:border-gray-800">
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                    {ip.cookies.map((c) => (
                      <tr key={c.name}>
                        <td className="w-36 shrink-0 px-2 py-1.5 font-mono text-gray-500 dark:text-gray-400">
                          {c.name}
                        </td>
                        <td className="px-2 py-1.5 font-mono text-gray-900 dark:text-gray-100 break-all">
                          {c.value}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : ip.cookiesEnabled ? (
              <p className="text-xs text-gray-400">No cookies stored yet.</p>
            ) : (
              <p className="text-xs text-gray-400">
                Enable cookies in identity config to persist session state per IP.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-gray-500 dark:text-gray-400 shrink-0">{label}</span>
      {children}
    </div>
  );
}

function IpPanel({ target }: { target: Target_ }) {
  const [{ data, fetching }] = useQuery({
    query: TARGET_IP_STATES_QUERY,
    variables: { targetName: target.name },
    requestPolicy: "cache-and-network",
  });
  const [selectedAddress, setSelectedAddress] = useState<string | null>(null);

  const states: IpRuntimeState[] = data?.targetIpStates ?? [];
  const quarantined = states.filter((s) => s.quarantined);
  const active = states.filter((s) => !s.quarantined);
  const selectedIp = states.find((s) => s.address === selectedAddress) ?? null;

  if (fetching && !states.length) {
    return (
      <div className="flex h-24 items-center justify-center">
        <Spinner />
      </div>
    );
  }

  if (!states.length) {
    return <p className="text-sm text-gray-400">No IPs resolved for this target.</p>;
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
            IPs ({states.length})
          </span>
          {quarantined.length > 0 && (
            <Badge variant="danger">{quarantined.length} quarantined</Badge>
          )}
        </div>
      </CardHeader>
      <div className="flex divide-x divide-gray-100 dark:divide-gray-800">
        <ul className="w-52 shrink-0 divide-y divide-gray-100 overflow-y-auto dark:divide-gray-800" style={{ maxHeight: 280 }}>
          {quarantined.map((ip) => (
            <IpRow
              key={ip.address}
              ip={ip}
              selected={selectedAddress === ip.address}
              onClick={() => setSelectedAddress(ip.address === selectedAddress ? null : ip.address)}
            />
          ))}
          {active.map((ip) => (
            <IpRow
              key={ip.address}
              ip={ip}
              selected={selectedAddress === ip.address}
              onClick={() => setSelectedAddress(ip.address === selectedAddress ? null : ip.address)}
            />
          ))}
        </ul>
        <div className="flex-1 min-w-0">
          {selectedIp ? (
            <IpDetailPane ip={selectedIp} threshold={target.ipFailuresUntilQuarantine} />
          ) : (
            <div className="flex h-full min-h-[120px] items-center justify-center text-xs text-gray-400">
              Select an IP
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function IpRow({
  ip,
  selected,
  onClick,
}: {
  ip: IpRuntimeState;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <li>
      <button
        onClick={onClick}
        className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
          selected
            ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
            : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
        }`}
      >
        <StatusDot status={ip.quarantined ? "unhealthy" : "healthy"} />
        <code className="truncate font-mono">{ip.address}</code>
        {ip.failures > 0 && (
          <span className="ml-auto shrink-0 text-amber-500">{ip.failures}✕</span>
        )}
      </button>
    </li>
  );
}

function TargetDetail({
  target,
  onEdit,
  onRemove,
}: {
  target: Target_;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="max-w-2xl">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {target.name}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {target.poolName ? `Pool: ${target.poolName}` : "No pool assigned"}
          </p>
        </div>
        <div className="flex gap-2">
          {target.static ? (
            <Badge variant="muted">static</Badge>
          ) : (
            <>
              <Button variant="secondary" size="sm" onClick={onEdit} disabled={!target.mutable}>
                Edit
              </Button>
              <Button variant="danger" size="sm" onClick={onRemove}>
                Remove
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="space-y-4">
        <Card>
          <CardHeader>
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              Configuration
            </span>
          </CardHeader>
          <CardBody className="space-y-2 text-sm">
            <Row label="URL Regex" value={target.regex} />
            <Row label="Min Request Interval" value={`${target.minRequestInterval}s`} />
            <Row label="Max Queue Wait" value={`${target.maxQueueWait}s`} />
            <Row label="Num Retries" value={String(target.numRetries)} />
            <Row label="IP Failures Until Quarantine" value={String(target.ipFailuresUntilQuarantine)} />
            <Row label="Quarantine Time" value={`${target.quarantineTime}s`} />
            <Row label="Default Proxy Port" value={String(target.defaultProxyPort)} />
            <Row label="Spoof User-Agent" value={target.spoofUserAgent ? "Yes" : "No"} />
            <Row label="Static" value={target.static ? "Yes" : "No"} />
            <Row label="Mutable" value={target.mutable ? "Yes" : "No"} />
          </CardBody>
        </Card>

        <IpPanel target={target} />

        <Card>
          <CardHeader>
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Metrics</span>
          </CardHeader>
          <CardBody>
            <MetricsPanel name={target.name} />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className="font-medium text-gray-900 dark:text-gray-100">{value}</span>
    </div>
  );
}

export function TargetsPage() {
  return (
    <Provider value={getClient()}>
      <TargetsInner />
    </Provider>
  );
}
