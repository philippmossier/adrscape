import { expect, it } from "vitest";
import { unseenModels } from "~/lib/models/seen";

it("announces no new models on a first visit", () => {
  expect(unseenModels(null, ["qwen3-0.6b", "gemma4-e2b"])).toEqual([]);
});
