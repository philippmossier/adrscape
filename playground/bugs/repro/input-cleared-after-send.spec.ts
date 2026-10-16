import { expect, test } from "@playwright/test";

test("the message box is empty after sending", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  await box.fill("hello");
  await box.press("Enter");
  await expect(page.getByText("Fake answer to: hello")).toBeVisible();
  await expect(box).toHaveValue("");
});
