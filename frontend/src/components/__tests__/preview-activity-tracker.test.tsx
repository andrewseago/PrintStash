/*
 * An administrator sees the previews the server is preparing on its own as one
 * Task, so an import that leaves many behind is not silent. It follows the live
 * queue, finishes when the queue empties, and is never kept across a reload.
 * Everyone else never reads the queue.
 */
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PreviewActivityTracker } from "@/components/preview-activity-tracker";
import * as jobsApi from "@/lib/api/jobs";
import * as taskCenter from "@/lib/task-center";
import { aJob } from "@/test-support/factories";

function mount(enabled: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <PreviewActivityTracker enabled={enabled} />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  window.localStorage.clear();
  taskCenter.resetTasksForNewSetup();
});

afterEach(() => {
  vi.restoreAllMocks();
  taskCenter.resetTasksForNewSetup();
});

describe("PreviewActivityTracker", () => {
  it("shows waiting previews as a running Task with its progress", async () => {
    vi.spyOn(jobsApi, "listWorkJobs").mockResolvedValue([
      aJob({ kind: "derivatives.mesh", state: "queued" }),
      aJob({ kind: "derivatives.mesh", state: "running" }),
      aJob({ kind: "ingestion.artifact_upload", state: "running" }),
    ]);

    mount(true);

    await waitFor(() => expect(taskCenter.listTasks()).toHaveLength(1));
    expect(taskCenter.listTasks()[0]).toMatchObject({
      title: "Preparing previews",
      status: "running",
      total: 2,
      processed: 0,
      ambient: true,
    });
  });

  it("completes the Task when the queue empties", async () => {
    const list = vi
      .spyOn(jobsApi, "listWorkJobs")
      .mockResolvedValueOnce([aJob({ kind: "derivatives.mesh", state: "running" })])
      .mockResolvedValue([aJob({ kind: "derivatives.mesh", state: "completed" })]);
    const client = mount(true);
    await waitFor(() => expect(taskCenter.listTasks()).toHaveLength(1));

    await client.invalidateQueries({ queryKey: ["work", "preview-activity"] });
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(taskCenter.listTasks()[0].status).toBe("completed"));
  });

  it("reads nothing for someone who is not an administrator", () => {
    const list = vi.spyOn(jobsApi, "listWorkJobs").mockResolvedValue([]);

    mount(false);

    expect(list).not.toHaveBeenCalled();
    expect(taskCenter.listTasks()).toHaveLength(0);
  });
});
