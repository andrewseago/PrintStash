/*
 * Storage insights name where bytes live. A source is either a folder this
 * computer mounts or remote storage, and the vault can itself be remote; the
 * totals split on that line because it is the one that decides what a disk
 * fills up with.
 */
import { describe, expect, it } from "vitest";

import type { StorageLocation } from "../api/storage-inventory";
import { bytesByWhere, placeKind } from "../storage-locations";

function place(over: Partial<StorageLocation>): StorageLocation {
  return {
    kind: "library",
    label: "x",
    transport: "mounted",
    remote: false,
    library_id: 1,
    connection: null,
    count: 0,
    logical_bytes: 0,
    unknown_size_count: 0,
    ...over,
  };
}

describe("placeKind", () => {
  it("tells a local vault from a remote one", () => {
    expect(placeKind(place({ kind: "vault", transport: "local" }))).toBe("vault-local");
    expect(placeKind(place({ kind: "vault", transport: "s3", remote: true }))).toBe("vault-remote");
  });

  it("tells a mounted folder from a remote source", () => {
    expect(placeKind(place({}))).toBe("mounted");
    expect(placeKind(place({ transport: "s3", remote: true }))).toBe("remote-source");
  });
});

describe("bytesByWhere", () => {
  it("adds up this computer apart from remote storage", () => {
    expect(
      bytesByWhere([
        place({ kind: "vault", logical_bytes: 400 }),
        place({ logical_bytes: 100 }),
        place({ transport: "s3", remote: true, logical_bytes: 5000 }),
        place({ transport: "webdav", remote: true, logical_bytes: 20 }),
      ]),
    ).toEqual({ here: 500, remote: 5020 });
  });

  it("is zero on both sides with no places", () => {
    expect(bytesByWhere([])).toEqual({ here: 0, remote: 0 });
  });
});
