import { expect, it } from "vitest";
import { chatReducer, initialState } from "~/lib/chat/store";

it("writes an answer into its own chat after the user opened another one", () => {
  let s = chatReducer(initialState, { type: "new", id: "c1", now: 1 });
  s = chatReducer(s, {
    type: "send",
    chatId: "c1",
    userId: "u1",
    assistantId: "a1",
    text: "Hello",
    now: 2,
  });
  s = chatReducer(s, { type: "new", id: "c2", now: 3 });
  s = chatReducer(s, { type: "token", chatId: "c1", messageId: "a1", text: "Hi there" });
  const stats = {
    tokens: 2,
    tokensPerSecond: 20,
    ttftMs: 1,
    totalMs: 2,
    interrupted: false,
  };
  s = chatReducer(s, { type: "finish", chatId: "c1", messageId: "a1", stats });
  const answer = s.chats.find((c) => c.id === "c1")!.messages[1]!;
  expect(answer.content).toBe("Hi there");
  expect(answer.stats).toEqual(stats);
});
