import { expect, test } from "@playwright/test";

test("a link in an answer opens in a new tab and keeps the chat", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  await box.fill("[docs](https://example.com/)");
  await box.press("Enter");
  const link = page.getByRole("link", { name: "docs" });
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", /noopener/);
});
