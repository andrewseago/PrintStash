import type { StorageLocation } from "@/lib/api/storage-inventory";

/** How a place describes itself under its name; the words come from the caller. */
export type PlaceKind = "vault-local" | "vault-remote" | "mounted" | "remote-source";

export function placeKind(place: StorageLocation): PlaceKind {
  if (place.kind === "vault") return place.remote ? "vault-remote" : "vault-local";
  return place.remote ? "remote-source" : "mounted";
}

export interface BytesByWhere {
  here: number;
  remote: number;
}

/** What is on each side of the line the user asked about: this computer, and elsewhere. */
export function bytesByWhere(places: readonly StorageLocation[]): BytesByWhere {
  let here = 0;
  let remote = 0;
  for (const place of places) {
    if (place.remote) remote += place.logical_bytes;
    else here += place.logical_bytes;
  }
  return { here, remote };
}
