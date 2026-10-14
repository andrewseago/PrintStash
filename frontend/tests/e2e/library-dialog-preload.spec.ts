/*
 * The tag dialog is a lazy chunk. Its first opening used to wait for the
 * download behind an empty frame, so the library warms the chunk once it is
 * usable instead of on the first click.
 */
import { expect, test } from "@playwright/test";

import { useMockApi } from "./_setup";

useMockApi();

test("the tag dialog's code is fetched before anyone opens it", async ({ page }) => {
  const chunk = page.waitForRequest(/model-tags-dialog/);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "All Models" })).toBeVisible();
  await chunk;
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
