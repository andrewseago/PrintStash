/*
 * What a reload brings back of the collection tree. The path to the open folder
 * always opens; of everything else, only the most recent few come back, because
 * restoring a whole session's expansion rebuilt a tree as tall as the library.
 */
import { describe, expect, it } from "vitest";

import { REMEMBERED_EXPANSIONS, restoredExpansions } from "../use-outliner-restoration";

describe("restoredExpansions", () => {
  it("opens the path to the current collection when nothing was saved", () => {
    expect([...restoredExpansions(null, ["a", "a/b"])]).toEqual(["a", "a/b"]);
    expect([...restoredExpansions(null, [])]).toEqual([]);
  });

  it("remembers everything opened while that fits the limit", () => {
    const saved = new Set(["x", "y"]);
    expect([...restoredExpansions(saved, ["a"])]).toEqual(["x", "y", "a"]);
  });

  it("keeps only the most recently opened folders beyond the limit", () => {
    const saved = new Set(Array.from({ length: 10 }, (_, index) => `f${index}`));
    const restored = restoredExpansions(saved, ["a", "a/b"]);
    expect([...restored]).toEqual([
      ...Array.from({ length: REMEMBERED_EXPANSIONS }, (_, index) => `f${index + 4}`),
      "a",
      "a/b",
    ]);
  });

  it("never lets the active path spend the remembered slots", () => {
    const saved = new Set(["old", "a", "a/b", "new"]);
    expect([...restoredExpansions(saved, ["a", "a/b"], 1)]).toEqual(["new", "a", "a/b"]);
  });
});
