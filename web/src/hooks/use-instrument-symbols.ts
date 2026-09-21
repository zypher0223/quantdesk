import { useEffect, useState } from "react";
import { fetchInstrumentIndex } from "../services/api";

/**
 * The fixed instrument universe, for a form that needs a symbol.
 *
 * Read once, never polled, and never guessed: while the gateway has not answered
 * — or is not running at all — the list stays empty and the caller keeps its
 * free-text input. That is why this returns the symbols alone: "no index yet"
 * and "no instruments" must look the same to the form.
 */
export function useInstrumentSymbols(): string[] {
  const [symbols, setSymbols] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const index = await fetchInstrumentIndex();
        if (!live) return;
        const list = [...new Set(index.instruments.map((item) => item.venueSymbol).filter(Boolean))].sort();
        if (list.length > 0) setSymbols(list);
      } catch {
        /* the caller falls back to typing a venue symbol */
      }
    })();
    return () => { live = false; };
  }, []);
  return symbols;
}
