/*
 * A tree row's count. It must not animate when it first appears (a whole tree
 * ticking in would read as a reload), must move toward a new value when it
 * changes, and must look provisional while it still describes the previous filters.
 */
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TreeCount } from "@/components/tree-count";

describe("TreeCount", () => {
  it("shows its value without animating on first render", () => {
    render(<TreeCount value={5} />);

    expect(screen.getByText("5")).not.toHaveClass("animate-count-up", "animate-count-down");
  });

  it("moves up when the value grows", () => {
    const { rerender } = render(<TreeCount value={5} />);

    rerender(<TreeCount value={9} />);

    expect(screen.getByText("9")).toHaveClass("animate-count-up");
  });

  it("moves down when the value shrinks", () => {
    const { rerender } = render(<TreeCount value={5} />);

    rerender(<TreeCount value={2} />);

    expect(screen.getByText("2")).toHaveClass("animate-count-down");
  });

  it("does not replay when re-rendered with the same value", () => {
    const { rerender } = render(<TreeCount value={5} />);
    rerender(<TreeCount value={9} />);
    const node = screen.getByText("9");

    rerender(<TreeCount value={9} stale />);

    expect(screen.getByText("9")).toBe(node);
  });

  it("is dimmed while it describes the previous filters", () => {
    render(<TreeCount value={5} stale />);

    expect(screen.getByText("5")).toHaveAttribute("data-stale", "true");
    expect(screen.getByText("5")).toHaveClass("opacity-50");
  });
});
