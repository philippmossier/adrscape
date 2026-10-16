import { expect, it } from "vitest";
import { deserialize, serialize, type ChatState } from "~/lib/chat/store";

it("reopens the chat that was open before the reload", () => {
  const chat = (id: string) => ({
    id,
    title: id,
    createdAt: 1,
    updatedAt: 1,
    messages: [{ id: `${id}-m`, role: "user" as const, content: "hi" }],
  });
  const state: ChatState = { chats: [chat("a"), chat("b")], activeId: "b" };
  expect(deserialize(serialize(state)).activeId).toBe("b");
});
