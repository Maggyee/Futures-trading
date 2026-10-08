import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
// Some development hosts configure a system HTTP proxy; local test traffic must bypass it.
process.env.NO_PROXY = [process.env.NO_PROXY, "127.0.0.1", "localhost"]
  .filter(Boolean)
  .join(",");
process.env.no_proxy = process.env.NO_PROXY;
const browserLibraries = resolve(
  "../.tools/browser-libs/usr/lib/x86_64-linux-gnu",
);
if (existsSync(browserLibraries))
  process.env.LD_LIBRARY_PATH = [browserLibraries, process.env.LD_LIBRARY_PATH]
    .filter(Boolean)
    .join(":");
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  timeout: 60000,
  use: {
    baseURL: "http://127.0.0.1:8001",
    viewport: { width: 1512, height: 982 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/python ../tests/browser_server.py",
    url: "http://127.0.0.1:8001/api/v1/health",
    reuseExistingServer: false,
    timeout: 120000,
  },
});
