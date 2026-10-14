import type { ComputeStatus } from "@/types/system";

/** The server can use a GPU only when it found a device and offers it for some operation. */
export function gpuAccessible(status: ComputeStatus): boolean {
  return status.device !== null && status.capabilities.some((capability) => capability.available);
}
