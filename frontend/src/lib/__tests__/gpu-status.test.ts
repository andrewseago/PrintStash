/*
 * "Accessible from the API" means the compute runtime found a device. Whether an
 * operation may use it yet (qualification, optional runtimes) is separate, so a
 * found device with nothing placed on it is accessible but idle, and
 * capabilities without a device never read as a reachable GPU.
 */
import { describe, expect, it } from "vitest";

import { gpuAccessible, gpuIdle } from "../gpu-status";
import { aComputeStatus } from "@/test-support/factories";

describe("GPU reachability", () => {
  it("is true when the runtime found a device", () => {
    expect(gpuAccessible(aComputeStatus())).toBe(true);
  });

  it("is false without a device, whatever the capabilities say", () => {
    expect(gpuAccessible(aComputeStatus({ device: null }))).toBe(false);
  });

  it("stays true when a device is found but no operation is placed on it yet", () => {
    const unqualified = aComputeStatus({
      capabilities: [{ operation: "render", available: false, reason: "unqualified" }],
    });
    expect(gpuAccessible(unqualified)).toBe(true);
    expect(gpuIdle(unqualified)).toBe(true);
  });

  it("is not idle once an operation can use the device, or when there is no device", () => {
    expect(gpuIdle(aComputeStatus())).toBe(false);
    expect(gpuIdle(aComputeStatus({ device: null }))).toBe(false);
  });
});
