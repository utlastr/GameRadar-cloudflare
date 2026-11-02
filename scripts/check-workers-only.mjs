import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

export function assertWorkersOnly(configuration) {
  if (configuration.containers?.length) {
    throw new Error(
      "Deployment blocked: this is the superseded Python/Containers prototype. " +
      "Complete the native JavaScript/TypeScript Workers migration before deploying.",
    );
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const configuration = JSON.parse(
      readFileSync(new URL("../wrangler.jsonc", import.meta.url), "utf8"),
    );
    assertWorkersOnly(configuration);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
