import { getJson, sendAction, type GetJsonOptions } from "@/lib/api/request";
import type { ComputeStatus } from "@/types/system";

export function restartPrintStash(): Promise<void> {
  return sendAction("/api/v1/system/restart", "POST");
}

/** Administrator-only: what the server's compute runtime can reach. */
export function getComputeStatus(options: GetJsonOptions = {}): Promise<ComputeStatus> {
  return getJson<ComputeStatus>("/api/v1/system/compute", options);
}
