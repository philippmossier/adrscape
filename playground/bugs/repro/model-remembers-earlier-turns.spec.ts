import { expect, test } from "@playwright/test";

test("the engine gets the earlier turns of the chat", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  for (const text of ["first", "second"]) {
    await box.fill(text);
    await box.press("Enter");
    await expect(page.getByText(`Fake answer to: ${text}`)).toBeVisible();
    await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
  }

  await box.fill("/history");
  await box.press("Enter");
  // The fake engine answers with the roles it received.
  await expect(page.getByText("Fake history: system user assistant user assistant user")).toBeVisible();
});
