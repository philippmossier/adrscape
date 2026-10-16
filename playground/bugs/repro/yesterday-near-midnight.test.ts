import { expect, it } from "vitest";
import { messageTime } from "~/lib/chat/time";

it("calls a message from 11:50 PM yesterday 'Yesterday', seen just after midnight", () => {
  const now = new Date(2026, 9, 5, 0, 10).getTime();
  const at = new Date(2026, 9, 4, 23, 50).getTime();
  expect(messageTime(at, now, "en")).toMatch(/^Yesterday 11:50\sPM$/);
});
