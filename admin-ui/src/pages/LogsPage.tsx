import { useState, useEffect, useRef, useCallback } from "react";
import { Pause, Play, Trash2 } from "lucide-react";
import { clsx } from "clsx";
import { Button } from "../components/ui/Button";
import { Badge } from "../components/ui/Badge";
import { getSession, resolveAdminUrl } from "../lib/auth";

interface RequestEvent {
  id: string;
  timestamp: number;
  target: string;
  method: string;
  url: string;
  proxy_ip: string;
  provider: string | null;
  status_code: number | null;
  success: boolean;
  attempt: number;
  elapsed_ms: number;
  error: string | null;
  request_headers: Record<string, string>;
  response_headers: Record<string, string>;
}

const MAX_EVENTS = 500;

function statusVariant(ev: RequestEvent): "success" | "warning" | "danger" | "muted" {
  if (ev.success) return "success";
  if (!ev.status_code) return "danger";
  if (ev.status_code >= 500) return "danger";
  if (ev.status_code >= 400) return "warning";
  return "muted";
}

function rowBg(selected: boolean) {
  return selected
    ? "bg-primary-50 dark:bg-primary-900/20"
    : "hover:bg-gray-50 dark:hover:bg-gray-800/50";
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-GB", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function truncateUrl(url: string, max = 60): string {
  if (url.length <= max) return url;
  return url.slice(0, max) + "…";
}

function EventDetail({ ev }: { ev: RequestEvent }) {
  return (
    <div className="flex flex-col gap-4 p-4 text-sm">
      <div>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-400">Overview</p>
        <div className="space-y-1.5">
          <DetailRow label="Target" value={ev.target} />
          <DetailRow label="Method" value={ev.method} />
          <DetailRow label="URL" value={ev.url} mono />
          <DetailRow label="Proxy IP" value={ev.proxy_ip} mono />
          {ev.provider && <DetailRow label="Provider" value={ev.provider} />}
          <DetailRow
            label="Status"
            value={
              ev.status_code
                ? `${ev.status_code} ${ev.success ? "(success)" : "(failed)"}`
                : ev.error
                  ? "Connection error"
                  : "—"
            }
          />
          <DetailRow label="Attempt" value={`#${ev.attempt + 1}`} />
          <DetailRow label="Elapsed" value={`${ev.elapsed_ms.toFixed(1)} ms`} />
          {ev.error && <DetailRow label="Error" value={ev.error} mono />}
        </div>
      </div>

      {Object.keys(ev.request_headers).length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-400">
            Request Headers
          </p>
          <HeaderTable headers={ev.request_headers} />
        </div>
      )}

      {Object.keys(ev.response_headers).length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-400">
            Response Headers
          </p>
          <HeaderTable headers={ev.response_headers} />
        </div>
      )}
    </div>
  );
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-3">
      <span className="w-24 shrink-0 text-gray-500 dark:text-gray-400">{label}</span>
      <span
        className={clsx(
          "min-w-0 break-all text-gray-900 dark:text-gray-100",
          mono && "font-mono text-xs",
        )}
      >
        {value}
      </span>
    </div>
  );
}

function HeaderTable({ headers }: { headers: Record<string, string> }) {
  return (
    <div className="overflow-x-auto rounded border border-gray-100 dark:border-gray-800">
      <table className="w-full text-xs">
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {Object.entries(headers).map(([k, v]) => (
            <tr key={k}>
              <td className="w-40 shrink-0 px-3 py-1.5 font-mono text-gray-500 dark:text-gray-400">
                {k}
              </td>
              <td className="px-3 py-1.5 font-mono text-gray-900 dark:text-gray-100 break-all">
                {v}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LogsPage() {
  const [events, setEvents] = useState<RequestEvent[]>([]);
  const [selected, setSelected] = useState<RequestEvent | null>(null);
  const [paused, setPaused] = useState(false);
  const [filterTarget, setFilterTarget] = useState("");
  const [connected, setConnected] = useState(false);

  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const bufferRef = useRef<RequestEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const tableRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  const flush = useCallback(() => {
    if (bufferRef.current.length === 0) return;
    setEvents((prev) => {
      const next = [...prev, ...bufferRef.current];
      bufferRef.current = [];
      return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
    });
  }, []);

  useEffect(() => {
    const session = getSession();
    const base = resolveAdminUrl();
    const params = new URLSearchParams();
    if (session?.token) params.set("token", session.token);
    params.set("limit", "200");

    const url = `${base}/events/stream?${params.toString()}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    es.onmessage = (e) => {
      try {
        const ev: RequestEvent = JSON.parse(e.data);
        if (pausedRef.current) {
          bufferRef.current.push(ev);
        } else {
          setEvents((prev) => {
            const next = [...prev, ev];
            return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
          });
        }
      } catch {
        // ignore parse errors
      }
    };

    return () => {
      es.close();
      esRef.current = null;
      setConnected(false);
    };
  }, []);

  // Auto-scroll table to bottom when not paused and new events arrive
  useEffect(() => {
    if (!paused && tableRef.current && atBottomRef.current) {
      tableRef.current.scrollTop = tableRef.current.scrollHeight;
    }
  }, [events, paused]);

  function handleScroll() {
    const el = tableRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  function handleResume() {
    setPaused(false);
    flush();
  }

  const targets = Array.from(new Set(events.map((e) => e.target))).sort();
  const displayed = filterTarget
    ? events.filter((e) => e.target === filterTarget)
    : events;

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-3 border-b border-gray-200 px-4 py-2 dark:border-gray-800">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">Request Log</span>

        <span
          className={clsx(
            "ml-1 h-2 w-2 rounded-full",
            connected ? "bg-green-500" : "bg-gray-400",
          )}
          title={connected ? "Connected" : "Disconnected"}
        />

        {/* Target filter */}
        <select
          className="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
          value={filterTarget}
          onChange={(e) => setFilterTarget(e.target.value)}
        >
          <option value="">All targets</option>
          {targets.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        <span className="text-xs text-gray-400">
          {displayed.length} event{displayed.length !== 1 ? "s" : ""}
          {paused && bufferRef.current.length > 0 && (
            <span className="ml-1 text-amber-500">
              (+{bufferRef.current.length} buffered)
            </span>
          )}
        </span>

        <div className="ml-auto flex gap-1">
          {paused ? (
            <Button variant="primary" size="sm" onClick={handleResume}>
              <Play className="h-3.5 w-3.5" />
              Resume
            </Button>
          ) : (
            <Button variant="secondary" size="sm" onClick={() => setPaused(true)}>
              <Pause className="h-3.5 w-3.5" />
              Pause
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { setEvents([]); setSelected(null); }}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Main content */}
      <div className="flex min-h-0 flex-1">
        {/* Event table */}
        <div
          ref={tableRef}
          onScroll={handleScroll}
          className="flex-1 overflow-y-auto font-mono text-xs"
        >
          {displayed.length === 0 ? (
            <div className="flex h-48 items-center justify-center text-sm text-gray-400">
              {connected ? "Waiting for requests…" : "Connecting…"}
            </div>
          ) : (
            <table className="w-full border-collapse">
              <thead className="sticky top-0 z-10 bg-white dark:bg-gray-950">
                <tr className="border-b border-gray-200 text-left text-xs font-medium text-gray-500 dark:border-gray-800 dark:text-gray-400">
                  <th className="px-3 py-1.5">Time</th>
                  <th className="px-3 py-1.5">Target</th>
                  <th className="px-3 py-1.5">Method</th>
                  <th className="px-3 py-1.5 max-w-xs">URL</th>
                  <th className="px-3 py-1.5">Proxy IP</th>
                  <th className="px-3 py-1.5">Status</th>
                  <th className="px-3 py-1.5">Try</th>
                  <th className="px-3 py-1.5 text-right">ms</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800/50">
                {displayed.map((ev) => (
                  <tr
                    key={ev.id}
                    onClick={() => setSelected(ev.id === selected?.id ? null : ev)}
                    className={clsx(
                      "cursor-pointer transition-colors",
                      rowBg(ev.id === selected?.id),
                    )}
                  >
                    <td className="px-3 py-1 text-gray-500">{formatTime(ev.timestamp)}</td>
                    <td className="px-3 py-1 text-gray-700 dark:text-gray-300">{ev.target}</td>
                    <td className="px-3 py-1 font-semibold text-gray-900 dark:text-gray-100">
                      {ev.method}
                    </td>
                    <td className="max-w-xs px-3 py-1 text-gray-700 dark:text-gray-300 truncate">
                      {truncateUrl(ev.url)}
                    </td>
                    <td className="px-3 py-1 text-gray-500">{ev.proxy_ip}</td>
                    <td className="px-3 py-1">
                      {ev.status_code !== null ? (
                        <span
                          className={clsx(
                            "font-semibold",
                            ev.success
                              ? "text-green-600 dark:text-green-400"
                              : ev.status_code >= 500
                                ? "text-red-600 dark:text-red-400"
                                : "text-amber-600 dark:text-amber-400",
                          )}
                        >
                          {ev.status_code}
                        </span>
                      ) : (
                        <span className="text-red-600 dark:text-red-400">ERR</span>
                      )}
                    </td>
                    <td className="px-3 py-1 text-gray-400">
                      {ev.attempt > 0 && (
                        <span className="text-amber-500">↺{ev.attempt}</span>
                      )}
                      {ev.attempt === 0 && "—"}
                    </td>
                    <td className="px-3 py-1 text-right text-gray-500">
                      {ev.elapsed_ms.toFixed(0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Detail pane */}
        {selected && (
          <div className="w-96 shrink-0 overflow-y-auto border-l border-gray-200 dark:border-gray-800">
            <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2 dark:border-gray-800">
              <div className="flex items-center gap-2">
                <Badge variant={statusVariant(selected)}>
                  {selected.status_code ?? "ERR"}
                </Badge>
                <span className="text-xs font-medium text-gray-700 dark:text-gray-300">
                  {selected.method} {selected.target}
                </span>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-xs text-gray-400 hover:text-gray-600"
              >
                ✕
              </button>
            </div>
            <EventDetail ev={selected} />
          </div>
        )}
      </div>
    </div>
  );
}
