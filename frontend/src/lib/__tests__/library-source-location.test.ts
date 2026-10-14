/*
 * What a source card says about where a source reads. A mounted source is its
 * folder; a remote source's own root path is an internal identifier
 * ("source://1/"), so a person is shown the connection, the bucket or host,
 * the connection's root and the source's own prefix instead.
 */
import { describe, expect, it } from "vitest";

import { describeSourceLocation } from "../library-source-location";
import { aStorageConnection, anExternalLibrary } from "@/test-support/factories";

describe("describeSourceLocation", () => {
  const connections = [
    aStorageConnection({
      id: 1,
      name: "R2Test",
      configuration: { bucket: "test", root: "PrintStash" },
    }),
    aStorageConnection({
      id: 2,
      name: "NAS",
      kind: "sftp",
      configuration: { host: "nas.local", root: "/models/" },
    }),
  ];

  it("is the folder of a mounted source", () => {
    const source = anExternalLibrary({ root_path: "/mnt/nas/3d", source_kind: "mounted" });

    expect(describeSourceLocation(source, connections)).toBe("/mnt/nas/3d");
  });

  it("describes a remote source by connection, bucket, root", () => {
    const source = anExternalLibrary({
      root_path: "source://1/",
      source_kind: "s3",
      connection_id: 1,
      source_prefix: "",
    });

    expect(describeSourceLocation(source, connections)).toBe("R2Test · test/PrintStash/");
  });

  it("appends the source's own prefix, tolerating stray slashes", () => {
    const source = anExternalLibrary({
      root_path: "source://2/",
      source_kind: "sftp",
      connection_id: 2,
      source_prefix: "/printers/",
    });

    expect(describeSourceLocation(source, connections)).toBe("NAS · nas.local/models/printers/");
  });

  it("falls back to the internal path when the connection is unknown", () => {
    const source = anExternalLibrary({
      root_path: "source://9/",
      source_kind: "s3",
      connection_id: 9,
    });

    expect(describeSourceLocation(source, connections)).toBe("source://9/");
  });
});
