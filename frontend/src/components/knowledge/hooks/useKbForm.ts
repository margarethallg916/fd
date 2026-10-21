import { useCallback, useMemo, useState } from "react";
import type {
  KBChunkPreset,
  KBConfig,
  KBCreate,
  KnowledgeBase,
} from "../../../services/api/knowledgeBase";
import {
  CHUNK_TOKEN_DEFAULT,
  CHUNK_TOKEN_MAX,
  CHUNK_TOKEN_MIN,
  OVERLAP_MAX,
  OVERLAP_MIN,
  getDefaultChunkSettings,
  parseBoundedInt,
} from "../knowledgeUtils";

export function useKbForm(kb: KnowledgeBase | null, config: KBConfig | null) {
  const initialPreset: KBChunkPreset = kb?.chunk_preset ?? "general";
  const initialChunk = kb
    ? { chunk_token_num: kb.chunk_token_num, overlap_percent: kb.overlap_percent }
    : getDefaultChunkSettings(config, initialPreset);

  const [name, setName] = useState(kb?.name ?? "");
  const [description, setDescription] = useState(kb?.description ?? "");
  const [preset, setPreset] = useState<KBChunkPreset>(initialPreset);
  const [tokenNum, setTokenNumRaw] = useState(
    String(initialChunk.chunk_token_num || CHUNK_TOKEN_DEFAULT),
  );
  const [overlap, setOverlapRaw] = useState(String(initialChunk.overlap_percent));
  const [advancedTouched, setAdvancedTouched] = useState(!!kb);

  const tokenValue = parseBoundedInt(tokenNum, CHUNK_TOKEN_MIN, CHUNK_TOKEN_MAX);
  const overlapValue = parseBoundedInt(overlap, OVERLAP_MIN, OVERLAP_MAX);
  const trimmedName = name.trim();
  const valid = trimmedName.length > 0 && tokenValue !== null && overlapValue !== null;

  const selectPreset = useCallback(
    (key: KBChunkPreset) => {
      setPreset(key);
      if (!advancedTouched) {
        const defaults = getDefaultChunkSettings(config, key);
        setTokenNumRaw(String(defaults.chunk_token_num));
        setOverlapRaw(String(defaults.overlap_percent));
      }
    },
    [advancedTouched, config],
  );

  const setTokenNum = useCallback((value: string) => {
    setTokenNumRaw(value);
    setAdvancedTouched(true);
  }, []);

  const setOverlap = useCallback((value: string) => {
    setOverlapRaw(value);
    setAdvancedTouched(true);
  }, []);

  const payload = useMemo<KBCreate | null>(() => {
    if (!valid || tokenValue === null || overlapValue === null) return null;
    return {
      name: trimmedName,
      description: description.trim(),
      chunk_preset: preset,
      chunk_token_num: tokenValue,
      overlap_percent: overlapValue,
    };
  }, [valid, trimmedName, description, preset, tokenValue, overlapValue]);

  const chunkChanged =
    !!kb &&
    (preset !== kb.chunk_preset ||
      tokenValue !== kb.chunk_token_num ||
      overlapValue !== kb.overlap_percent);

  const dirty =
    !!kb &&
    (trimmedName !== kb.name ||
      description.trim() !== kb.description ||
      chunkChanged);

  return {
    name,
    setName,
    description,
    setDescription,
    preset,
    selectPreset,
    tokenNum,
    setTokenNum,
    overlap,
    setOverlap,
    tokenValid: tokenValue !== null,
    overlapValid: overlapValue !== null,
    valid,
    payload,
    chunkChanged,
    dirty,
  };
}

export type KbFormState = ReturnType<typeof useKbForm>;
