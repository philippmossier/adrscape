import { expect, it } from "vitest";
import { classifyHost } from "~/lib/privacy/network";

it("does not count a look-alike of hf.co as a model host", () => {
  expect(classifyHost("evilhf.co", "chat.example.com")).toBe("other");
  expect(classifyHost("cas-bridge.xethub.hf.co", "chat.example.com")).toBe("model");
});
