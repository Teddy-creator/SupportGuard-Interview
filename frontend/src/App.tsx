import {
  useCallback,
  useEffect,
  useState,
} from "react";

import { ApprovalPage } from "./pages/ApprovalPage";
import { ConversationPage } from "./pages/ConversationPage";
import {
  bootstrapSession,
  sessionBootstrapErrorMessage,
} from "./productApi";
import type { SessionContext } from "./productTypes";
import { navigate, useRoute } from "./routing";

export function App() {
  const route = useRoute();
  const [boot, setBoot] = useState<{
    csrf: string;
    context: SessionContext;
  } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    void bootstrapSession()
      .then(setBoot)
      .catch((value) => setError(sessionBootstrapErrorMessage(value)));
  }, []);
  useEffect(() => {
    if (!boot) return;
    const role = boot.context.principal.role;
    if (role === "approver" && route.page !== "approvals") {
      navigate("/approvals", true);
      return;
    }
    if (role === "customer" && route.page === "approvals")
      navigate("/conversations/new", true);
  }, [boot, route.page]);
  const update = useCallback(
    (csrf: string, context: SessionContext) => setBoot({ csrf, context }),
    [],
  );
  const failClosedSession = useCallback((message: string) => {
    setBoot(null);
    setError(message);
  }, []);
  if (error)
    return (
      <main className="fatal-state">
        <span className="brand-mark">SG</span>
        <h1>暂时无法打开 SupportGuard</h1>
        <p>{error}</p>
        <button onClick={() => window.location.reload()}>重新连接</button>
      </main>
    );
  if (!boot)
    return (
      <main className="fatal-state">
        <span className="brand-mark">SG</span>
        <p>正在建立安全会话…</p>
      </main>
    );
  const scopeKey = `${boot.context.principal.id}:${boot.context.active_tenant.id}`;
  return boot.context.principal.role === "approver" ? (
    <ApprovalPage
      key={scopeKey}
      selected={route.page === "approvals" ? route.id : undefined}
      session={boot.context}
      csrf={boot.csrf}
      onSession={update}
      onNavigate={navigate}
      onSessionFailure={failClosedSession}
    />
  ) : (
    <ConversationPage
      key={scopeKey}
      session={boot.context}
      csrf={boot.csrf}
      onSession={update}
      onSessionFailure={failClosedSession}
    />
  );
}

export default App;
