/*
 * Where a folder's cards arrive from. Drilling in comes from the right, climbing
 * out from the left, and anything unrelated fades. The classification must not
 * mistake a shared name prefix ("house" / "house2") for ancestry, and the
 * entrances stay horizontal or opacity-only so reading-position anchors hold.
 */
import { describe, expect, it } from "vitest";

import { FOLDER_CARDS_CLASS, FOLDER_MOVE_CLASS, folderMove } from "../folder-transition";

describe("folderMove", () => {
  it("is forward when drilling from the vault or a folder into a descendant", () => {
    expect(folderMove("", "maraio")).toBe("forward");
    expect(folderMove("house", "house/garden")).toBe("forward");
  });

  it("is back when climbing to an ancestor or the vault", () => {
    expect(folderMove("house/garden", "house")).toBe("back");
    expect(folderMove("house", "")).toBe("back");
  });

  it("is a jump between unrelated paths, never because of a shared name prefix", () => {
    expect(folderMove("house", "garage")).toBe("jump");
    expect(folderMove("house", "house2")).toBe("jump");
    expect(folderMove("house/garden", "garage/tools")).toBe("jump");
  });

  it("rejects a move that goes nowhere", () => {
    expect(() => folderMove("house", "house")).toThrow("two different paths");
  });

  it("only offers horizontal or opacity-only entrances", () => {
    expect(FOLDER_MOVE_CLASS).toEqual({
      forward: "folder-in-forward",
      back: "folder-in-back",
      jump: "fade-in",
    });
  });

  it("names a card entrance for every move", () => {
    expect(FOLDER_CARDS_CLASS).toEqual({
      forward: "folder-cards-forward",
      back: "folder-cards-back",
      jump: "folder-cards-jump",
    });
  });
});
