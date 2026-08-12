let accessToken: string | null = null;

/**
 * In-memory adapter for a production OIDC host. The token is never persisted,
 * placed in a URL, or included in an error. A real login shell can call this
 * before rendering SupportGuard; development demo sessions leave it unset.
 */
export function setSupportGuardAccessToken(token: string | null): void {
  const normalized = token?.trim() ?? "";
  accessToken = normalized || null;
}

export function bearerHeaders(): Record<string, string> {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}
