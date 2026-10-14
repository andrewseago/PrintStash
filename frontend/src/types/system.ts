/** Mirrors `ComputeStatus` in `backend/app/runtime/compute/contracts.py` (the fields Settings shows). */
export type ComputeMode = "auto" | "cpu";
export type ComputeOperation = "render" | "dense" | "sparse";
export type ComputeReason =
  | "ready"
  | "preview"
  | "disabled"
  | "runtime_missing"
  | "adapter_missing"
  | "software_adapter"
  | "unqualified"
  | "device_failed"
  | "capacity"
  | "deadline"
  | "invalid_input"
  | "cancelled"
  | "broker_unavailable";

export interface ComputeDevice {
  identity: string;
  name: string;
  vendor_id: number;
  device_id: number;
  driver: string;
  backend: string;
}

export interface ComputeCapability {
  operation: ComputeOperation;
  available: boolean;
  reason: ComputeReason;
}

export interface ComputeStatus {
  mode: ComputeMode;
  device: ComputeDevice | null;
  capabilities: ComputeCapability[];
  budget_bytes: number;
  reserved_bytes: number;
  resident_entries: number;
  queue_depth: number;
  completed: number;
  fallbacks: number;
  cold_start_seconds: number;
}
