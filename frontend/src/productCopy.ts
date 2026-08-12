const actionNames: Record<string, string> = {
  refund: "退款申请",
  api_key_revocation: "撤销 API Key",
  entitlement_change: "调整账号配额",
};

export function actionLabel(actionType: string): string {
  return actionNames[actionType] ?? "受保护业务操作";
}

export function actionSummary(
  actionType: string,
  payload: Record<string, unknown>,
): string {
  if (actionType === "refund" && payload.amount !== undefined) {
    const currency =
      typeof payload.currency === "string" ? ` ${payload.currency}` : "";
    return `${String(payload.amount)}${currency}`;
  }
  const value = payload.api_key_id ?? payload.subscription_id;
  return value === undefined ? "等待人工核验" : String(value);
}
