import { useQuery } from "urql";
import { Globe, Layers, Target } from "lucide-react";
import { Card, CardBody, CardHeader } from "../components/ui/Card";
import { Spinner } from "../components/ui/Spinner";
import { STATUS_QUERY, PROVIDERS_QUERY, POOLS_QUERY, TARGETS_QUERY } from "../graphql/queries";
import { getClient } from "../lib/client";
import { Provider } from "urql";

function StatCard({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType;
  label: string;
  value: string | number | undefined;
}) {
  return (
    <Card>
      <CardBody className="flex items-center gap-4">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary-50 dark:bg-primary-900/20">
          <Icon className="h-5 w-5 text-primary-600 dark:text-primary-400" />
        </div>
        <div>
          <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
          <p className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            {value ?? "—"}
          </p>
        </div>
      </CardBody>
    </Card>
  );
}

function HomeInner() {
  const [statusResult] = useQuery({ query: STATUS_QUERY });
  const [providersResult] = useQuery({ query: PROVIDERS_QUERY });
  const [poolsResult] = useQuery({ query: POOLS_QUERY });
  const [targetsResult] = useQuery({ query: TARGETS_QUERY });

  const status: { authEnabled: boolean; userSub: string; userRole: string } | undefined =
    statusResult.data?.status;
  const providers = providersResult.data?.providers ?? [];
  const pools = poolsResult.data?.pools ?? [];
  const targets = targetsResult.data?.targets ?? [];

  const loading =
    statusResult.fetching ||
    providersResult.fetching ||
    poolsResult.fetching ||
    targetsResult.fetching;

  if (loading && !status) {
    return (
      <div className="flex h-48 items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Overview</h1>
      </div>

      <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard icon={Globe} label="Proxy Providers" value={providers.length} />
        <StatCard icon={Layers} label="IP Pools" value={pools.length} />
        <StatCard icon={Target} label="Targets" value={targets.length} />
      </div>

      <div className="grid gap-6 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <h2 className="text-sm font-medium text-gray-900 dark:text-gray-100">Server Info</h2>
          </CardHeader>
          <CardBody className="space-y-2 text-sm">
            <Row label="Auth" value={status?.authEnabled ? "Enabled" : "Disabled"} />
            <Row label="User" value={status?.userSub} />
            <Row label="Role" value={status?.userRole} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="text-sm font-medium text-gray-900 dark:text-gray-100">Quick Stats</h2>
          </CardHeader>
          <CardBody className="space-y-2 text-sm">
            <Row
              label="Total IPs"
              value={pools.reduce(
                (n: number, p: { ipRequests: { count: number }[] }) =>
                  n + (p.ipRequests?.reduce((m, r) => m + r.count, 0) ?? 0),
                0,
              )}
            />
            <Row
              label="Static entities"
              value={
                [...providers, ...pools, ...targets].filter(
                  (e: { static: boolean }) => e.static,
                ).length
              }
            />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | number | undefined }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className="font-medium text-gray-900 dark:text-gray-100">{value ?? "—"}</span>
    </div>
  );
}


export function Home() {
  return (
    <Provider value={getClient()}>
      <HomeInner />
    </Provider>
  );
}
