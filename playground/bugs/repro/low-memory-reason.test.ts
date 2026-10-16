import { expect, it } from "vitest";
import { reasonText } from "~/lib/labels";

it("explains low memory as low memory, not as a phone", () => {
  const text = reasonText("hw.lowMemory", 0);
  expect(text).toMatch(/memory/i);
  expect(text).not.toMatch(/phones/i);
});
