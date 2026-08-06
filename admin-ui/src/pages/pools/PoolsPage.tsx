import { useState } from "react";
import { useQuery, useMutation, Provider } from "urql";
import { Plus, RefreshCw, Lock, Layers, Trash2 } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Badge } from "../../components/ui/Badge";
import { Card, CardBody, CardHeader } from "../../components/ui/Card";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { Spinner } from "../../components/ui/Spinner";
import { POOLS_QUERY, PROVIDERS_QUERY, POOL_IP_HEALTH_QUERY } from "../../graphql/queries";
import { ADD_POOL, UPDATE_POOL, REMOVE_POOL } from "../../graphql/mutations";
import { Combobox } from "../../components/ui/Combobox";
import { getClient } from "../../lib/client";

interface IpRequest {
  provider: string;
  count: number;
}

interface Pool {
  name: string;
  static: boolean;
  mutable: boolean;
  ipRequests: IpRequest[];
}

interface IpHealth {
  address: string;
  provider: string | null;
  status: string | null; // "up" | "down" | null (unknown)
}

type PoolFormValue = { name: string; rows: IpRequest[] };

const EMPTY_FORM: PoolFormValue = { name: "", rows: [{ provider: "", count: 10 }] };

function PoolForm({
  initial,
  providerNames,
  onSave,
  onCancel,
  saving,
  error,
}: {
  initial: PoolFormValue;
  providerNames: string[];
  onSave: (v: PoolFormValue) => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
}) {
  const [form, setForm] = useState<PoolFormValue>(initial);

  function updateRow(i: number, k: keyof IpRequest, v: string | number) {
    setForm((f) => {
      const rows = [...f.rows];
      rows[i] = { ...rows[i], [k]: v };
      return { ...f, rows };
    });
  }

  function addRow() {
    setForm((f) => ({ ...f, rows: [...f.rows, { provider: "", count: 10 }] }));
  }

  function removeRow(i: number) {
    setForm((f) => ({ ...f, rows: f.rows.filter((_, j) => j !== i) }));
  }

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

      <div className="flex flex-col gap-1">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
          Provider References
        </label>
        <div className="space-y-2">
          {form.rows.map((row, i) => (
            <div key={i} className="flex items-center gap-2">
              <Combobox
                options={providerNames}
                value={row.provider}
                onChange={(v) => updateRow(i, "provider", v)}
                placeholder="provider name"
                required
              />
              <input
                className="w-20 rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
                type="number"
                min={1}
                placeholder="count"
                value={row.count}
                onChange={(e) => updateRow(i, "count", parseInt(e.target.value, 10) || 1)}
                required
              />
              {form.rows.length > 1 && (
                <button
                  type="button"
                  onClick={() => removeRow(i)}
                  className="rounded p-1 text-gray-400 hover:text-red-500"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              )}
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={addRow}
          className="mt-1 flex items-center gap-1 text-xs text-primary-600 hover:text-primary-700 dark:text-primary-400"
        >
          <Plus className="h-3 w-3" /> Add provider
        </button>
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

function PoolsInner() {
  const [{ data, fetching }, refetch] = useQuery({ query: POOLS_QUERY });
  const [{ data: providersData }] = useQuery({ query: PROVIDERS_QUERY });
  const [, addPool] = useMutation(ADD_POOL);
  const [, updatePool] = useMutation(UPDATE_POOL);
  const [, removePool] = useMutation(REMOVE_POOL);

  const [selected, setSelected] = useState<Pool | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mutError, setMutError] = useState<string | null>(null);

  const pools: Pool[] = data?.pools ?? [];
  const providerNames: string[] = (providersData?.providers ?? []).map(
    (p: { name: string }) => p.name,
  );

  async function handleAdd(form: PoolFormValue) {
    setSaving(true);
    setMutError(null);
    const res = await addPool({
      input: {
        name: form.name,
        ipRequests: form.rows,
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

  async function handleUpdate(form: PoolFormValue) {
    setSaving(true);
    setMutError(null);
    const res = await updatePool({
      input: {
        name: form.name,
        ipRequests: form.rows,
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
    if (!confirm(`Remove pool "${name}"?`)) return;
    const res = await removePool({ name });
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
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">IP Pools</span>
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

        {fetching && !pools.length ? (
          <div className="flex h-24 items-center justify-center">
            <Spinner />
          </div>
        ) : (
          <ul className="scrollbar-thin overflow-y-auto">
            {pools.map((p) => (
              <li key={p.name}>
                <button
                  className={`flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors ${
                    selected?.name === p.name
                      ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
                      : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  }`}
                  onClick={() => setSelected(p)}
                >
                  <Layers className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                  <span className="truncate">{p.name}</span>
                  <span className="ml-auto shrink-0 text-xs text-gray-400">
                    {p.ipRequests.reduce((n, r) => n + r.count, 0)} IPs
                  </span>
                  {p.static && <Lock className="h-3 w-3 shrink-0 text-gray-400" />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <div className="flex-1 overflow-auto p-6">
        {selected ? (
          <PoolDetail
            pool={selected}
            onEdit={() => { setShowEdit(true); setMutError(null); }}
            onRemove={() => handleRemove(selected.name)}
          />
        ) : (
          <div className="flex h-48 items-center justify-center text-sm text-gray-400">
            Select a pool
          </div>
        )}
      </div>

      <Dialog open={showAdd} onClose={() => setShowAdd(false)} title="Add IP Pool">
        <PoolForm
          initial={EMPTY_FORM}
          providerNames={providerNames}
          onSave={handleAdd}
          onCancel={() => setShowAdd(false)}
          saving={saving}
          error={mutError}
        />
      </Dialog>

      {selected && (
        <Dialog open={showEdit} onClose={() => setShowEdit(false)} title="Edit IP Pool">
          <PoolForm
            initial={{
              name: selected.name,
              rows: selected.ipRequests.length
                ? selected.ipRequests
                : [{ provider: "", count: 10 }],
            }}
            providerNames={providerNames}
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

function PoolDetail({
  pool,
  onEdit,
  onRemove,
}: {
  pool: Pool;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const totalIps = pool.ipRequests.reduce((n, r) => n + r.count, 0);

  const [{ data: healthData }] = useQuery({
    query: POOL_IP_HEALTH_QUERY,
    variables: { poolName: pool.name },
    requestPolicy: "cache-and-network",
  });
  const health: IpHealth[] = healthData?.poolIpHealth ?? [];
  const healthByProvider = new Map<string, IpHealth[]>();
  for (const h of health) {
    if (!h.provider) continue;
    const list = healthByProvider.get(h.provider) ?? [];
    list.push(h);
    healthByProvider.set(h.provider, list);
  }

  return (
    <div className="max-w-lg">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">{pool.name}</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {totalIps} IP{totalIps !== 1 ? "s" : ""} across {pool.ipRequests.length} provider{pool.ipRequests.length !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="flex gap-2">
          {pool.static ? (
            <Badge variant="muted">static</Badge>
          ) : (
            <>
              <Button variant="secondary" size="sm" onClick={onEdit} disabled={!pool.mutable}>
                Edit
              </Button>
              <Button variant="danger" size="sm" onClick={onRemove}>
                Remove
              </Button>
            </>
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Provider References</span>
        </CardHeader>
        <CardBody className="p-0">
          <ul className="divide-y divide-gray-100 dark:divide-gray-800">
            {pool.ipRequests.map((r) => {
              const rows = healthByProvider.get(r.provider) ?? [];
              const knownRows = rows.filter((h) => h.status !== null);
              const healthy = rows.filter((h) => h.status === "up").length;
              return (
                <li key={r.provider} className="flex items-center justify-between px-4 py-2 text-sm">
                  <code className="font-mono text-gray-900 dark:text-gray-100">{r.provider}</code>
                  <div className="flex items-center gap-2">
                    {knownRows.length > 0 && (
                      <Badge variant={healthy === rows.length ? "success" : healthy === 0 ? "danger" : "warning"}>
                        {healthy}/{rows.length} healthy
                      </Badge>
                    )}
                    <span className="text-xs text-gray-400">{r.count} IP{r.count !== 1 ? "s" : ""}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}

export function PoolsPage() {
  return (
    <Provider value={getClient()}>
      <PoolsInner />
    </Provider>
  );
}
