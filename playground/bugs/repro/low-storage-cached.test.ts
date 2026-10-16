import { expect, it } from "vitest";
import { PROFILES } from "~/lib/hardware/fixtures";
import { recommend } from "~/lib/hardware/recommend";

it("never warns about storage for a model that is already downloaded", () => {
  const tight = { ...PROFILES.hiddenVendorFastGpu, storageFreeMB: 2000 };
  const options = recommend(tight, new Set(["gemma4-e2b"])).options;
  expect(options.find((o) => o.model.id === "gemma4-e2b")?.lowStorage).toBe(false);
});
