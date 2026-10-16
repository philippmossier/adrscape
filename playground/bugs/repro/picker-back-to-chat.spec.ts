import { expect, test } from "@playwright/test";

test("Back from 'Change model' returns to the running chat", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();

  await page.getByTitle("Change model").click();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(box).toBeEditable();
});
