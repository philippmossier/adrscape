import { expect, test } from "@playwright/test";

test("'New chat' opens an empty chat after a conversation", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();
  const box = page.getByRole("textbox", { name: "Message" });
  await expect(box).toBeEditable();
  await box.fill("hello");
  await box.press("Enter");
  await expect(page.getByText("Fake answer to: hello")).toBeVisible();

  await page.getByRole("button", { name: "New chat" }).first().click();
  await expect(page.getByText("What can I help with?")).toBeVisible();
  await expect(page.getByText("Fake answer to: hello")).toHaveCount(0);
});
