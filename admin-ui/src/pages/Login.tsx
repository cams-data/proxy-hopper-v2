import { useState, useEffect } from "react";
import { Moon, Sun } from "lucide-react";
import { Input } from "../components/ui/Input";
import { Button } from "../components/ui/Button";
import { Spinner } from "../components/ui/Spinner";
import { loginWithPassword, probeAdminServer, saveSession, resolveAdminUrl } from "../lib/auth";
import { resetClient } from "../lib/client";

interface LoginProps {
  onLogin: () => void;
  dark: boolean;
  onToggleDark: () => void;
}

export function Login({ onLogin, dark, onToggleDark }: LoginProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [authEnabled, setAuthEnabled] = useState<boolean | null>(null);

  const adminUrl = resolveAdminUrl();

  useEffect(() => {
    probeAdminServer(adminUrl).then(({ reachable, authEnabled: enabled }) => {
      if (!reachable) {
        setError(`Cannot reach admin server at ${adminUrl}`);
        return;
      }
      if (!enabled) {
        saveSession({ adminUrl, token: null });
        resetClient();
        onLogin();
        return;
      }
      setAuthEnabled(true);
    });
  }, [adminUrl, onLogin]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const token = await loginWithPassword(adminUrl, username, password);
      saveSession({ adminUrl, token });
      resetClient();
      onLogin();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
      <button
        onClick={onToggleDark}
        className="absolute right-4 top-4 rounded p-1.5 text-gray-500 hover:bg-gray-200 dark:text-gray-400 dark:hover:bg-gray-800"
        aria-label="Toggle dark mode"
      >
        {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
      </button>

      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold text-primary-600 dark:text-primary-400">
            Proxy Hopper
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Admin Portal</p>
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-900">
          {authEnabled === null && !error ? (
            <div className="flex items-center justify-center py-4">
              <Spinner />
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <Input
                label="Username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
              />
              <Input
                label="Password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />

              {error && (
                <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
                  {error}
                </p>
              )}

              <Button type="submit" variant="primary" disabled={loading} className="mt-2">
                {loading ? <Spinner className="h-4 w-4" /> : null}
                Sign in
              </Button>
            </form>
          )}
        </div>

        <p className="mt-4 text-center text-xs text-gray-400 dark:text-gray-600">
          {adminUrl}
        </p>
      </div>
    </div>
  );
}
