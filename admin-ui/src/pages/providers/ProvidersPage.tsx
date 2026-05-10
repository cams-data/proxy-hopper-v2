import { useState } from "react";
import { useQuery, useMutation, Provider } from "urql";
import { Plus, RefreshCw, Lock } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Badge } from "../../components/ui/Badge";
import { Card, CardBody, CardHeader } from "../../components/ui/Card";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { Spinner } from "../../components/ui/Spinner";
import { PROVIDERS_QUERY } from "../../graphql/queries";
import {
  ADD_PROVIDER,
  UPDATE_PROVIDER,
  REMOVE_PROVIDER,
} from "../../graphql/mutations";
import { getClient } from "../../lib/client";

interface Provider_ {
  name: string;
  ipList: string[];
  regionTag: string | null;
  hasAuth: boolean;
  static: boolean;
  mutable: boolean;
}

const EMPTY_FORM = {
  name: "",
  ips: "",
  regionTag: "",
  username: "",
  password: "",
};

function ProviderForm({
  initial,
  onSave,
  onCancel,
  saving,
  error,
}: {
  initial: typeof EMPTY_FORM;
  onSave: (v: typeof EMPTY_FORM) => void;
  onCancel: () => void;
  saving: boolean;
  error: string | null;
}) {
  const [form, setForm] = useState(initial);
  const set = (k: keyof typeof EMPTY_FORM) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(form);
      }}
    >
      <Input label="Name" value={form.name} onChange={set("name")} required disabled={!!initial.name} />
      <div className="flex flex-col gap-1">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
          IP Addresses (one per line, optionally host:port)
        </label>
        <textarea
          className="rounded-md border border-gray-200 bg-white px-3 py-2 font-mono text-sm text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
          rows={6}
          value={form.ips}
          onChange={(e) => setForm((f) => ({ ...f, ips: e.target.value }))}
          placeholder={"1.2.3.4:8080\n5.6.7.8:3128"}
          required
        />
      </div>
      <Input label="Region Tag (optional)" value={form.regionTag} onChange={set("regionTag")} />
      <div className="border-t border-gray-100 pt-3 dark:border-gray-800">
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
          Auth (optional)
        </p>
        <div className="grid grid-cols-2 gap-3">
          <Input label="Username" value={form.username} onChange={set("username")} autoComplete="off" />
          <Input label="Password" type="password" value={form.password} onChange={set("password")} autoComplete="new-password" />
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

function parseIpList(ips: string): string[] {
  return ips
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function ProvidersInner() {
  const [{ data, fetching }, refetch] = useQuery({ query: PROVIDERS_QUERY });
  const [, addProvider] = useMutation(ADD_PROVIDER);
  const [, updateProvider] = useMutation(UPDATE_PROVIDER);
  const [, removeProvider] = useMutation(REMOVE_PROVIDER);

  const [selected, setSelected] = useState<Provider_ | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mutError, setMutError] = useState<string | null>(null);

  const providers: Provider_[] = data?.providers ?? [];

  async function handleAdd(form: typeof EMPTY_FORM) {
    setSaving(true);
    setMutError(null);
    const res = await addProvider({
      input: {
        name: form.name,
        ipList: parseIpList(form.ips),
        regionTag: form.regionTag || null,
        auth:
          form.username
            ? { username: form.username, password: form.password }
            : null,
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
    const res = await updateProvider({
      input: {
        name: form.name,
        ipList: parseIpList(form.ips),
        regionTag: form.regionTag || null,
        auth:
          form.username
            ? { username: form.username, password: form.password }
            : null,
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
    if (!confirm(`Remove provider "${name}"?`)) return;
    const res = await removeProvider({ name });
    if (res.error) alert(res.error.message);
    else {
      setSelected(null);
      refetch({ requestPolicy: "network-only" });
    }
  }

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="w-64 shrink-0 border-r border-gray-200 dark:border-gray-800">
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-800">
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
            Proxy Providers
          </span>
          <div className="flex gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch({ requestPolicy: "network-only" })}
              aria-label="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" onClick={() => { setShowAdd(true); setMutError(null); }}>
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {fetching && !providers.length ? (
          <div className="flex h-24 items-center justify-center">
            <Spinner />
          </div>
        ) : (
          <ul className="scrollbar-thin overflow-y-auto">
            {providers.map((p) => (
              <li key={p.name}>
                <button
                  className={`flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm transition-colors ${
                    selected?.name === p.name
                      ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
                      : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  }`}
                  onClick={() => setSelected(p)}
                >
                  <Globe2 className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                  <span className="truncate">{p.name}</span>
                  <span className="ml-auto shrink-0 text-xs text-gray-400">
                    {p.ipList.length}
                  </span>
                  {p.static && <Lock className="h-3 w-3 shrink-0 text-gray-400" />}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {/* Detail */}
      <div className="flex-1 overflow-auto p-6">
        {selected ? (
          <ProviderDetail
            provider={selected}
            onEdit={() => { setShowEdit(true); setMutError(null); }}
            onRemove={() => handleRemove(selected.name)}
          />
        ) : (
          <div className="flex h-48 items-center justify-center text-sm text-gray-400">
            Select a provider
          </div>
        )}
      </div>

      {/* Add dialog */}
      <Dialog open={showAdd} onClose={() => setShowAdd(false)} title="Add Provider">
        <ProviderForm
          initial={EMPTY_FORM}
          onSave={handleAdd}
          onCancel={() => setShowAdd(false)}
          saving={saving}
          error={mutError}
        />
      </Dialog>

      {/* Edit dialog */}
      {selected && (
        <Dialog open={showEdit} onClose={() => setShowEdit(false)} title="Edit Provider">
          <ProviderForm
            initial={{
              name: selected.name,
              ips: selected.ipList.join("\n"),
              regionTag: selected.regionTag ?? "",
              username: "",
              password: "",
            }}
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

function Globe2({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>;
}

function ProviderDetail({
  provider,
  onEdit,
  onRemove,
}: {
  provider: Provider_;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="max-w-lg">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            {provider.name}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {provider.ipList.length} IP{provider.ipList.length !== 1 ? "s" : ""}
            {provider.regionTag ? ` · ${provider.regionTag}` : ""}
          </p>
        </div>
        <div className="flex gap-2">
          {provider.static ? (
            <Badge variant="muted">static</Badge>
          ) : (
            <>
              <Button variant="secondary" size="sm" onClick={onEdit} disabled={!provider.mutable}>
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
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Details</span>
          </CardHeader>
          <CardBody className="space-y-2 text-sm">
            <Row label="Region" value={provider.regionTag ?? "—"} />
            <Row label="Auth" value={provider.hasAuth ? "Configured" : "None"} />
            <Row label="Mutable" value={provider.mutable ? "Yes" : "No"} />
            <Row label="Static" value={provider.static ? "Yes" : "No"} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              IP Addresses ({provider.ipList.length})
            </span>
          </CardHeader>
          <CardBody className="p-0">
            <ul className="divide-y divide-gray-100 dark:divide-gray-800 max-h-64 overflow-y-auto">
              {provider.ipList.map((ip) => (
                <li key={ip} className="px-4 py-2">
                  <code className="font-mono text-sm text-gray-900 dark:text-gray-100">{ip}</code>
                </li>
              ))}
            </ul>
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

export function ProvidersPage() {
  return (
    <Provider value={getClient()}>
      <ProvidersInner />
    </Provider>
  );
}
