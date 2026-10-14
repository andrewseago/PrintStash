import type {
  ExternalLibrary,
  StorageConnection,
  StorageConnectionConfigurationValue,
} from "@/types";

function text(value: StorageConnectionConfigurationValue | undefined): string {
  return value === undefined || value === null ? "" : String(value).trim();
}

function join(...parts: string[]): string {
  return parts
    .map((part) => part.replace(/^\/+|\/+$/g, ""))
    .filter((part) => part !== "")
    .join("/");
}

/**
 * What a source card shows as "where". A mounted source is its folder. A remote
 * source's own `root_path` is an internal identifier (`source://1/`) that says
 * nothing to a person, so show the connection and the place it reads: the
 * bucket or host, the connection's root, and the source's own prefix.
 */
export function describeSourceLocation(
  source: Pick<ExternalLibrary, "root_path" | "source_kind" | "connection_id" | "source_prefix">,
  connections: readonly StorageConnection[],
): string {
  if ((source.source_kind ?? "mounted") === "mounted") return source.root_path;
  const connection = connections.find((row) => row.id === source.connection_id);
  if (connection === undefined) return source.root_path;
  const configuration = connection.configuration;
  const place = text(configuration.bucket) || text(configuration.host) || text(configuration.url);
  const path = join(place, text(configuration.root), source.source_prefix ?? "");
  return path === "" ? connection.name : `${connection.name} · ${path}/`;
}
