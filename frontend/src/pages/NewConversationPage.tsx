import type { SessionContext } from "../productTypes";

export function NewConversationPage({
  session,
  onPick,
}: {
  session: SessionContext;
  onPick: (value: string) => void;
}) {
  const demoResourcesAvailable =
    session.auth_mode === "development" &&
    session.active_tenant.id === "tenant_demo" &&
    session.customer?.id === "cust_demo";
  const starters = demoResourcesAvailable
    ? [
        {
          label: "为什么余额充足仍然返回 429？",
          prompt:
            "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded，为什么？",
        },
        {
          label: "检查一笔疑似重复扣费",
          prompt: "请检查 bill_demo_duplicate 是否为重复扣费，并按政策处理。",
        },
      ]
    : [
        {
          label: "排查 API 返回 429",
          prompt:
            "我的 API 请求返回 429。请告诉我需要补充哪些请求信息，并协助排查。",
        },
        {
          label: "核验一笔疑似重复扣费",
          prompt:
            "我怀疑账户存在重复扣费。请告诉我需要提供哪些账单信息，并协助核验。",
        },
      ];
  return (
    <div className="conversation-empty">
      <span className="empty-mark">SG</span>
      <h2>今天想解决什么问题？</h2>
      <p>
        描述产品、用量、账单或安全问题。SupportGuard
        会先核验事实，再给出带来源的回答。
      </p>
      <div className="starter-grid">
        {starters.map((starter) => (
          <button key={starter.label} onClick={() => onPick(starter.prompt)}>
            {starter.label}
          </button>
        ))}
      </div>
    </div>
  );
}

