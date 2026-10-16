import { expect, test } from "@playwright/test";

const chat = (id: string, title: string, updatedAt: number) => ({
  id,
  title,
  createdAt: updatedAt,
  updatedAt,
  messages: [{ id: `${id}-m`, role: "user", content: title }],
});

test("the sidebar lists the most recently used chat first", async ({ page }) => {
  await page.addInitScript(
    (state) => {
      localStorage.setItem("local-ai-chat:v1", state);
    },
    JSON.stringify({
      chats: [chat("a", "Older chat", 1000), chat("b", "Newer chat", 2000)],
      activeId: "a",
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();

  const items = page.locator('[data-sidebar="sidebar"]').first().locator("li");
  await expect(items.first()).toContainText("Newer chat");
  await expect(items.nth(1)).toContainText("Older chat");
});
