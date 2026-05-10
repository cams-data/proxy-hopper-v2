import { NavLink } from "react-router-dom";
import { LogOut, Moon, Sun } from "lucide-react";
import { clsx } from "clsx";
import { clearSession } from "../../lib/auth";
import { resetClient } from "../../lib/client";

const NAV_LINKS = [
  { to: "/", label: "Overview", end: true },
  { to: "/providers", label: "Proxy Providers" },
  { to: "/pools", label: "IP Pools" },
  { to: "/targets", label: "Targets" },
  { to: "/logs", label: "Live Logs" },
];

interface NavBarProps {
  onLogout: () => void;
  dark: boolean;
  onToggleDark: () => void;
}

export function NavBar({ onLogout, dark, onToggleDark }: NavBarProps) {

  function handleLogout() {
    clearSession();
    resetClient();
    onLogout();
  }

  return (
    <header className="flex h-12 shrink-0 items-center border-b border-gray-200 bg-white px-4 dark:border-gray-800 dark:bg-gray-950">
      <span className="mr-6 font-semibold text-primary-600 dark:text-primary-400">
        Proxy Hopper
      </span>

      <nav className="flex items-center gap-1">
        {NAV_LINKS.map(({ to, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              clsx(
                "rounded px-3 py-1.5 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
                  : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800",
              )
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>

      <div className="ml-auto flex items-center gap-1">
        <button
          onClick={onToggleDark}
          className="rounded p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
          aria-label="Toggle dark mode"
        >
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
        <button
          onClick={handleLogout}
          className="rounded p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
          aria-label="Log out"
        >
          <LogOut className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}
