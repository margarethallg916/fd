import { useCallback, useEffect, useRef } from "react";
import {
  POLL_BASE_MS,
  POLL_MAX_MS,
  nextPollDelay,
} from "../knowledgeUtils";

interface UseAdaptivePollingOptions {
  /** Polling runs only while true (something is still processing). */
  enabled: boolean;
  /** Fetch fresh data and return a fingerprint of it; a changed fingerprint resets the backoff. */
  refresh: () => Promise<string>;
  baseMs?: number;
  maxMs?: number;
}

/**
 * Chained-setTimeout poll with ×1.5 backoff while results are unchanged.
 * `trigger(delayMs)` requests an early refresh (after an upload/retry or a WS event)
 * and restarts the backoff from the base interval.
 */
export function useAdaptivePolling({
  enabled,
  refresh,
  baseMs = POLL_BASE_MS,
  maxMs = POLL_MAX_MS,
}: UseAdaptivePollingOptions) {
  const refreshRef = useRef(refresh);
  const enabledRef = useRef(enabled);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dueAtRef = useRef(0);
  const delayRef = useRef(baseMs);
  const signatureRef = useRef<string | null>(null);
  const runningRef = useRef(false);
  const dirtyRef = useRef(false);
  const unmountedRef = useRef(false);
  const runRef = useRef<() => Promise<void>>(async () => undefined);

  useEffect(() => {
    refreshRef.current = refresh;
    enabledRef.current = enabled;
  });

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const schedule = useCallback(
    (ms: number) => {
      clear();
      dueAtRef.current = Date.now() + ms;
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        void runRef.current();
      }, ms);
    },
    [clear],
  );

  useEffect(() => {
    runRef.current = async () => {
      if (unmountedRef.current) return;
      if (runningRef.current) {
        dirtyRef.current = true;
        return;
      }
      runningRef.current = true;
      try {
        const signature = await refreshRef.current();
        const changed = signature !== signatureRef.current;
        signatureRef.current = signature;
        delayRef.current = nextPollDelay(
          delayRef.current,
          changed,
          baseMs,
          maxMs,
        );
      } catch {
        delayRef.current = nextPollDelay(delayRef.current, false, baseMs, maxMs);
      } finally {
        runningRef.current = false;
      }
      if (unmountedRef.current) return;
      if (dirtyRef.current) {
        dirtyRef.current = false;
        schedule(300);
      } else if (enabledRef.current) {
        schedule(delayRef.current);
      }
    };
  }, [baseMs, maxMs, schedule]);

  useEffect(() => {
    unmountedRef.current = false;
    if (enabled) {
      delayRef.current = baseMs;
      schedule(baseMs);
    } else {
      clear();
    }
    return clear;
  }, [enabled, baseMs, schedule, clear]);

  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      clear();
    };
  }, [clear]);

  const trigger = useCallback(
    (delayMs = 0) => {
      delayRef.current = baseMs;
      const due = Date.now() + delayMs;
      if (timerRef.current !== null && dueAtRef.current <= due) return;
      schedule(delayMs);
    },
    [baseMs, schedule],
  );

  return { trigger };
}
