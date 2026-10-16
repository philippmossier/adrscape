import { expect, test } from "@playwright/test";

test("Stop can be pressed while an answer is written, and stops it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  // The fake engine echoes word by word, 30 ms apart: 100 words take about 3 s.
  await box.fill(Array.from({ length: 100 }, (_, i) => `w${i}`).join(" "));
  await box.press("Enter");
  const stop = page.getByRole("button", { name: "Stop" });
  await expect(stop).toBeEnabled();
  await stop.click();
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible({ timeout: 1000 });
  // Stopped early: the answer never reaches the last word.
  await expect(page.locator(".md").last()).not.toContainText("w99");
});
