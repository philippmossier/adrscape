import { expect, test } from "@playwright/test";

const chat = (id: string, title: string, updatedAt: number) => ({
  id,
  title,
  createdAt: updatedAt,
  updatedAt,
  messages: [{ id: `${id}-m`, role: "user", content: title }],
});

test("deleting a chat in the sidebar deletes that chat, not the open one", async ({
  page,
}) => {
  await page.addInitScript(
    (state) => {
      if (!sessionStorage.getItem("seeded")) {
        localStorage.setItem("local-ai-chat:v1", state);
        sessionStorage.setItem("seeded", "1");
      }
    },
    JSON.stringify({
      chats: [chat("b", "Current chat", 2000), chat("a", "Old chat", 1000)],
      activeId: "b",
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: /start/i }).first().click();

  const sidebar = page.locator('[data-sidebar="sidebar"]').first();
  const old = sidebar.locator("li", { hasText: "Old chat" });
  await old.hover();
  await old.getByRole("button", { name: "Delete chat" }).click();
  await old.getByRole("button", { name: "Confirm delete" }).click();

  await expect(sidebar.getByText("Old chat")).toHaveCount(0);
  await expect(sidebar.getByText("Current chat")).toBeVisible();
});
