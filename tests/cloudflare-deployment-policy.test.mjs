import assert from "node:assert/strict";
import test from "node:test";
import { assertWorkersOnly } from "../scripts/check-workers-only.mjs";

test("rejects the superseded container deployment configuration", () => {
  assert.throws(
    () => assertWorkersOnly({ containers: [{ class_name: "RadarApp" }] }),
    /Deployment blocked/,
  );
});

test("allows a configuration without containers", () => {
  assert.doesNotThrow(() => assertWorkersOnly({ main: "src/worker.ts" }));
});
