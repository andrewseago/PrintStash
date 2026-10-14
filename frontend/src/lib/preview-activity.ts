import type { JobStatus } from "@/types";

const ACTIVE = new Set<JobStatus["state"]>(["queued", "running", "interrupted"]);

/** Previews, thumbnails and 3D views being prepared: every derivative Job still to finish. */
export function previewJobsRemaining(jobs: readonly Pick<JobStatus, "kind" | "state">[]): number {
  return jobs.filter((job) => job.kind.startsWith("derivatives.") && ACTIVE.has(job.state)).length;
}

export interface PreviewProgress {
  /** The most Jobs seen waiting during this burst of work. */
  peak: number;
  done: number;
  percent: number;
}

/** Progress of a burst: it grows when more Jobs arrive and finishes at zero remaining. */
export function previewProgress(peak: number, remaining: number): PreviewProgress {
  const top = Math.max(peak, remaining);
  const done = top - remaining;
  return { peak: top, done, percent: top === 0 ? 100 : (done / top) * 100 };
}
