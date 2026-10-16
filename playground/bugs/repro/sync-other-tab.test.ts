import { expect, it } from "vitest";
import { chatReducer, type ChatState } from "~/lib/chat/store";

const chat = (id: string, content = "hi", updatedAt = 1) => ({
  id,
  title: id,
  createdAt: 1,
  updatedAt,
  messages: [{ id: `${id}-m`, role: "user" as const, content }],
});

it("drops a chat that was deleted in the other tab", () => {
  const local: ChatState = { chats: [chat("a"), chat("b")], activeId: "b" };
  const incoming: ChatState = { chats: [chat("b")], activeId: "b" };
  const next = chatReducer(local, { type: "sync", incoming, keepChatId: null });
  expect(next.chats.map((c) => c.id)).toEqual(["b"]);
});

it("takes newer messages the other tab wrote into a shared chat", () => {
  const local: ChatState = { chats: [chat("a", "old")], activeId: "a" };
  const incoming: ChatState = { chats: [chat("a", "new", 2)], activeId: "a" };
  const next = chatReducer(local, { type: "sync", incoming, keepChatId: null });
  expect(next.chats[0]!.messages[0]!.content).toBe("new");
});

it("still keeps the chat this tab is writing an answer into", () => {
  const local: ChatState = { chats: [chat("a", "streaming...")], activeId: "a" };
  const incoming: ChatState = { chats: [chat("a", "old")], activeId: "a" };
  const next = chatReducer(local, { type: "sync", incoming, keepChatId: "a" });
  expect(next.chats[0]!.messages[0]!.content).toBe("streaming...");
});
