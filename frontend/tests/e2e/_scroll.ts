import type { Page } from "@playwright/test";

/** The next page of a Library view loads as its main container is scrolled to the end. */
export async function scrollToEnd(page: Page): Promise<void> {
  // Scrolling an empty container reaches nothing: wait for the first page.
  await page.locator("main [data-library-entry]").first().waitFor();
  await page.getByRole("main").evaluate((main) => {
    main.scrollTop = main.scrollHeight;
  });
}
