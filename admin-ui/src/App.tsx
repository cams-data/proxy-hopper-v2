import { useState, useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { Login } from "./pages/Login";
import { Home } from "./pages/Home";
import { ProvidersPage } from "./pages/providers/ProvidersPage";
import { PoolsPage } from "./pages/pools/PoolsPage";
import { TargetsPage } from "./pages/targets/TargetsPage";
import { LogsPage } from "./pages/LogsPage";
import { getSession } from "./lib/auth";
import { useDarkMode } from "./hooks/useDarkMode";

export function App() {
  const [authed, setAuthed] = useState(() => getSession() !== null);
  const { dark, toggle } = useDarkMode();

  useEffect(() => {
    if (!authed) return;
    const session = getSession();
    if (!session) setAuthed(false);
  }, [authed]);

  if (!authed) {
    return <Login onLogin={() => setAuthed(true)} dark={dark} onToggleDark={toggle} />;
  }

  return (
    <BrowserRouter>
      <Shell onLogout={() => setAuthed(false)} dark={dark} onToggleDark={toggle}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/pools" element={<PoolsPage />} />
          <Route path="/targets" element={<TargetsPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
