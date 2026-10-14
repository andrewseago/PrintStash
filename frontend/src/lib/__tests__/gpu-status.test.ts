import { describe, expect, it } from "vitest";

import { gpuAccessible } from "../gpu-status";
import { aComputeStatus } from "@/test-support/factories";

describe("gpuAccessible", () => {
  it("is true with a device offering at least one operation", () => {
    expect(gpuAccessible(aComputeStatus())).toBe(true);
  });

  it("is false without a device, whatever the capabilities say", () => {
    expect(gpuAccessible(aComputeStatus({ device: null }))).toBe(false);
  });

  it("is false when a device is found but nothing is available on it", () => {
    const none = aComputeStatus({
      capabilities: [{ operation: "render", available: false, reason: "software_adapter" }],
    });
    expect(gpuAccessible(none)).toBe(false);
  });
});
