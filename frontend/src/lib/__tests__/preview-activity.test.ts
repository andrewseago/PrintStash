/*
 * The previews the server prepares by itself. Only derivative Jobs that have not
 * finished count, and a burst's progress is measured against the most that was
 * waiting during it, so a task that grows while it runs never reports more than
 * it has done.
 */
import { describe, expect, it } from "vitest";

import { previewJobsRemaining, previewProgress } from "../preview-activity";

describe("previewJobsRemaining", () => {
  it("counts derivative Jobs that are waiting, running or interrupted", () => {
    expect(
      previewJobsRemaining([
        { kind: "derivatives.mesh", state: "queued" },
        { kind: "derivatives.gcode", state: "running" },
        { kind: "derivatives.viewer_stl", state: "interrupted" },
      ]),
    ).toBe(3);
  });

  it("ignores finished derivative Jobs plus every other kind of work", () => {
    expect(
      previewJobsRemaining([
        { kind: "derivatives.mesh", state: "completed" },
        { kind: "derivatives.mesh", state: "failed" },
        { kind: "derivatives.mesh", state: "cancelled" },
        { kind: "sources.scan", state: "running" },
        { kind: "backups.create", state: "queued" },
      ]),
    ).toBe(0);
  });
});

describe("previewProgress", () => {
  it("starts at nothing done", () => {
    expect(previewProgress(0, 40)).toEqual({ peak: 40, done: 0, percent: 0 });
  });

  it("advances as Jobs finish", () => {
    expect(previewProgress(40, 10)).toEqual({ peak: 40, done: 30, percent: 75 });
  });

  it("widens the burst when more Jobs arrive than were first seen", () => {
    expect(previewProgress(40, 55)).toEqual({ peak: 55, done: 0, percent: 0 });
  });

  it("is complete when nothing remains", () => {
    expect(previewProgress(0, 0).percent).toBe(100);
  });
});
