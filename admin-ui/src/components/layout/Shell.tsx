import { NavBar } from "./NavBar";

interface ShellProps {
  children: React.ReactNode;
  onLogout: () => void;
  dark: boolean;
  onToggleDark: () => void;
}

export function Shell({ children, onLogout, dark, onToggleDark }: ShellProps) {
  return (
    <div className="flex h-screen flex-col overflow-hidden bg-gray-50 dark:bg-gray-950">
      <NavBar onLogout={onLogout} dark={dark} onToggleDark={onToggleDark} />
      <main className="flex h-full min-h-0 flex-1 flex-col overflow-hidden">{children}</main>
    </div>
  );
}
