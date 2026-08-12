import { defineConfig } from "@playwright/test";

export const V1512_PREFLIGHT_E2E_FILE =
  "candidate-stack-preflight-v1512.spec.ts";
export const V1512_PREFLIGHT_EXPECTED_TESTS = 19;

const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
const browserChannel = executablePath || process.env.CI ? undefined : "chrome";

export default defineConfig({
  testDir: "./e2e",
  testMatch: V1512_PREFLIGHT_E2E_FILE,
  outputDir:
    process.env.PLAYWRIGHT_OUTPUT_DIR ?? "./test-results/v1512-preflight",
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  metadata: {
    candidateStack: true,
    apiMockingAllowed: false,
    expectedTests: V1512_PREFLIGHT_EXPECTED_TESTS,
  },
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173",
    channel: browserChannel,
    launchOptions: executablePath ? { executablePath } : undefined,
    headless: true,
    trace: "retain-on-failure",
  },
});
