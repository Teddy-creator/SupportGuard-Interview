import { defineConfig } from "@playwright/test";

export const CURRENT_E2E_FILE = "conversation-v15.spec.ts";
export const CURRENT_E2E_EXPECTED_TESTS = 19;

const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;
const browserChannel = executablePath || process.env.CI ? undefined : "chrome";

export default defineConfig({
  testDir: "./e2e",
  testMatch: CURRENT_E2E_FILE,
  testIgnore: "**/golden-scenarios.spec.ts",
  metadata: {
    currentE2EFile: CURRENT_E2E_FILE,
    currentE2EExpectedTests: CURRENT_E2E_EXPECTED_TESTS,
  },
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "./test-results",
  timeout: 45_000,
  fullyParallel: false,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173",
    channel: browserChannel,
    launchOptions: executablePath ? { executablePath } : undefined,
    headless: true,
    trace: "retain-on-failure",
  },
});
