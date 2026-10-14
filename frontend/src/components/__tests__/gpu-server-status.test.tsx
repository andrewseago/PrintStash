/** The Server GPU card tells an administrator whether the API can reach a GPU. */
import { useQuery } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GpuServerStatus } from "../gpu-server-status";
import { settingsComputeOptions } from "@/lib/queries/settings-system";
import { aComputeStatus } from "@/test-support/factories";
import { json, renderApp } from "@/test-support/render";

function Card() {
  return <GpuServerStatus read={useQuery(settingsComputeOptions())} />;
}

describe("GpuServerStatus", () => {
  it("shows the reachable device with its capabilities", async () => {
    renderApp(<Card />, { routes: { "GET /api/v1/system/compute": json(aComputeStatus()) } });

    expect(await screen.findByText("GPU accessible from the API")).toBeInTheDocument();
    expect(screen.getByText("NVIDIA GeForce RTX 4070")).toBeInTheDocument();
    expect(screen.getByText("Vulkan")).toBeInTheDocument();
    expect(screen.getByText("0x10de / 0x2786")).toBeInTheDocument();
    expect(screen.getByText("256 MB of 1 GB in use")).toBeInTheDocument();
    expect(screen.getByText("42 done · 1 queued · 3 CPU fallbacks")).toBeInTheDocument();
    expect(screen.getByText("Rendering").parentElement).toHaveTextContent("Available");
    expect(screen.getByText("Dense AI").parentElement).toHaveTextContent("Not qualified yet");
  });

  it("explains why no GPU is reachable", async () => {
    renderApp(<Card />, {
      routes: {
        "GET /api/v1/system/compute": json(
          aComputeStatus({
            device: null,
            capabilities: [
              { operation: "render", available: false, reason: "runtime_missing" },
              { operation: "dense", available: false, reason: "runtime_missing" },
              { operation: "sparse", available: false, reason: "runtime_missing" },
            ],
          }),
        ),
      },
    });

    expect(await screen.findByText("No GPU accessible from the API")).toBeInTheDocument();
    expect(
      screen.getByText(/GPU runtime is not installed · Work runs on the CPU/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Device")).not.toBeInTheDocument();
  });

  it("reports a failed read instead of claiming there is no GPU", async () => {
    renderApp(<Card />, {
      routes: { "GET /api/v1/system/compute": json({ detail: "boom" }, 500) },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not read the server's GPU status.",
    );
    expect(screen.queryByText("No GPU accessible from the API")).not.toBeInTheDocument();
  });
});
