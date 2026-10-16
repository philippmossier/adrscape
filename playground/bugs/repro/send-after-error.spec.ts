import { expect, test } from "@playwright/test";

test("after a failed answer the next message is answered", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  await box.fill("/error");
  await box.press("Enter");
  await expect(page.getByText(/Something went wrong/)).toBeVisible();

  await box.fill("hello");
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
  await box.press("Enter");
  await expect(page.getByText("Fake answer to: hello")).toBeVisible();
});
