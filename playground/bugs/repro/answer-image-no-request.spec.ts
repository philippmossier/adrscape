import { expect, test } from "@playwright/test";

test("an image in an answer does not load from another server", async ({
  page,
  context,
}) => {
  const foreign: string[] = [];
  context.on("request", (r) => {
    if (new URL(r.url()).host === "tracker.example") foreign.push(r.url());
  });
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  await box.fill("![pixel](https://tracker.example/p.png)");
  await box.press("Enter");
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
  await expect(page.getByText("pixel").last()).toBeVisible();
  await page.waitForTimeout(500);
  expect(foreign).toEqual([]);
});
