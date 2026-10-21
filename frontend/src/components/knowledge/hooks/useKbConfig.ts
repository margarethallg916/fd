import { useEffect, useState } from "react";
import {
  knowledgeBaseApi,
  type KBConfig,
} from "../../../services/api/knowledgeBase";

export function useKbConfig() {
  const [config, setConfig] = useState<KBConfig | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    knowledgeBaseApi
      .getConfig()
      .then((res) => {
        if (!cancelled) setConfig(res);
      })
      .catch((e) => {
        if (!cancelled) setError(e);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { config, error };
}
