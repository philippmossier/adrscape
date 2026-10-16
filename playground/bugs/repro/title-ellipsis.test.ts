import { expect, it } from "vitest";
import { titleFrom } from "~/lib/chat/store";

it("keeps a title that fits exactly, without an ellipsis", () => {
  const text = "x".repeat(42);
  expect(titleFrom(text)).toBe(text);
});
