/** How the user moved between two folders, which decides where the new
 *  folder's content settles in from. */
export type FolderMove = "forward" | "back" | "jump";

const isInside = (path: string, ancestor: string) =>
  ancestor === "" || path.startsWith(`${ancestor}/`);

/** Drilling into a descendant is `forward`, climbing to an ancestor is `back`,
 *  anything else (sibling, search, a deep link) is a `jump`. */
export function folderMove(from: string, to: string): FolderMove {
  if (from === to) throw new Error("folderMove needs two different paths");
  if (isInside(to, from)) return "forward";
  if (isInside(from, to)) return "back";
  return "jump";
}

/** Horizontal or opacity-only: a vertical offset would move the entries that
 *  reading-position restoration measures. */
export const FOLDER_MOVE_CLASS = {
  forward: "folder-in-forward",
  back: "folder-in-back",
  jump: "fade-in",
} satisfies Record<FolderMove, string>;

/** The grid's cards enter one after another (a capped 30ms stagger) instead of
 *  the container moving as a block, so a folder whose cards were all cached
 *  still reads as arriving. */
export const FOLDER_CARDS_CLASS = {
  forward: "folder-cards-forward",
  back: "folder-cards-back",
  jump: "folder-cards-jump",
} satisfies Record<FolderMove, string>;
