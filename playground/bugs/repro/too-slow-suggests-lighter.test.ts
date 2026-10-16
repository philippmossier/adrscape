import { expect, it } from "vitest";
import { adviseFromSpeed } from "~/lib/hardware/advice";
import { PROFILES } from "~/lib/hardware/fixtures";
import { recommend } from "~/lib/hardware/recommend";
import { getModel } from "~/lib/models/catalog";

it("suggests the next lighter model when the answer is too slow", () => {
  const options = recommend(PROFILES.appleSiliconMac).options;
  expect(adviseFromSpeed(3, 80, getModel("gemma4-e2b")!, options)).toEqual({
    kind: "too-slow",
    suggest: getModel("qwen3-1.7b"),
  });
});
