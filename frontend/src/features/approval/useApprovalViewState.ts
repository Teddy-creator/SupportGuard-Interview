import { useCallback, useEffect, useState } from "react";

export function useApprovalViewState({
  scopeKey,
  cancelSource,
}: {
  scopeKey: string;
  cancelSource: () => void;
}) {
  const [sourceOpen, setSourceOpen] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSourceOpen(false);
  }, [scopeKey]);

  const openSource = useCallback(() => setSourceOpen(true), []);
  const closeSource = useCallback(() => {
    cancelSource();
    setSourceOpen(false);
  }, [cancelSource]);

  return { sourceOpen, openSource, closeSource };
}
