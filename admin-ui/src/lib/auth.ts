const STORAGE_KEY = "ph_admin_session";

export interface Session {
  adminUrl: string;
  token: string | null; // null = auth disabled
}

export function getSession(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
}

export function clearSession(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export function getAdminUrl(): string {
  return (
    getSession()?.adminUrl ??
    (window as unknown as { __PROXY_HOPPER_ADMIN_URL__?: string }).__PROXY_HOPPER_ADMIN_URL__ ??
    import.meta.env.VITE_ADMIN_URL ??
    "http://localhost:8081"
  );
}

export function resolveAdminUrl(): string {
  return (
    (window as unknown as { __PROXY_HOPPER_ADMIN_URL__?: string }).__PROXY_HOPPER_ADMIN_URL__ ??
    import.meta.env.VITE_ADMIN_URL ??
    ""
  );
}

export function getAuthHeaders(): Record<string, string> {
  const session = getSession();
  if (session?.token) {
    return { Authorization: `Bearer ${session.token}` };
  }
  return {};
}

// Login via username/password (OAuth2 form-encoded)
export async function loginWithPassword(
  adminUrl: string,
  username: string,
  password: string,
): Promise<string> {
  const body = new URLSearchParams({ username, password });
  const res = await fetch(`${adminUrl}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(
      (detail as { detail?: string }).detail ?? `Login failed (${res.status})`,
    );
  }
  const data = (await res.json()) as { access_token: string };
  return data.access_token;
}

// Check if the admin server is reachable and auth is enabled
export async function probeAdminServer(
  adminUrl: string,
): Promise<{ reachable: boolean; authEnabled: boolean }> {
  try {
    const res = await fetch(`${adminUrl}/health`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) return { reachable: false, authEnabled: false };

    // Try a GraphQL status query to see if auth is on
    const gqlRes = await fetch(`${adminUrl}/graphql`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "{ status { authEnabled } }" }),
      signal: AbortSignal.timeout(5000),
    });
    if (gqlRes.ok) {
      const gqlData = (await gqlRes.json()) as {
        data?: { status?: { authEnabled?: boolean } };
      };
      const authEnabled = gqlData.data?.status?.authEnabled ?? false;
      return { reachable: true, authEnabled };
    }
    // If GQL fails (401), auth is likely enabled
    return { reachable: true, authEnabled: gqlRes.status === 401 };
  } catch {
    return { reachable: false, authEnabled: false };
  }
}
