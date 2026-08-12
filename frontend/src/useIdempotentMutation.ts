import { useCallback, useRef, useState } from "react";

import { createIdempotencyKey } from "./idempotency";
import { isAbortError, ProductApiError } from "./productApi";

type MutationState = {
  busy: boolean;
  retryable: boolean;
};

/**
 * Keeps one stable idempotency key across an ambiguous failure and its retry.
 * A successful mutation consumes the key; callers decide how to surface errors.
 */
export function useIdempotentMutation() {
  const pending = useRef<{ identity: string; key: string } | null>(null);
  const inFlight = useRef(false);
  const [state, setState] = useState<MutationState>({
    busy: false,
    retryable: false,
  });

  const run = useCallback(
    async <T>(identity: string, operation: (key: string) => Promise<T>) => {
      if (inFlight.current) throw new Error("mutation_in_progress");
      inFlight.current = true;
      const current =
        pending.current?.identity === identity
          ? pending.current
          : { identity, key: createIdempotencyKey() };
      pending.current = current;
      setState({ busy: true, retryable: false });
      try {
        const result = await operation(current.key);
        pending.current = null;
        inFlight.current = false;
        setState({ busy: false, retryable: false });
        return result;
      } catch (error) {
        const retryable =
          !isAbortError(error) &&
          (error instanceof ProductApiError
            ? error.retryable
            : error instanceof TypeError);
        if (!retryable) pending.current = null;
        inFlight.current = false;
        setState({ busy: false, retryable });
        throw error;
      }
    },
    [],
  );

  const reset = useCallback(() => {
    pending.current = null;
    inFlight.current = false;
    setState({ busy: false, retryable: false });
  }, []);

  return { ...state, run, reset };
}
