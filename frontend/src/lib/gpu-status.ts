import type { ComputeStatus } from "@/types/system";

/** The API reaches a GPU when its compute runtime found a device. Whether any
 *  operation is allowed to use it yet is a separate fact, shown per capability. */
export function gpuAccessible(status: ComputeStatus): boolean {
  return status.device !== null;
}

/** A device was found but no operation is placed on it (unqualified, runtime missing…). */
export function gpuIdle(status: ComputeStatus): boolean {
  return gpuAccessible(status) && !status.capabilities.some((capability) => capability.available);
}
