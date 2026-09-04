#!/usr/bin/env node
/**
 * browser-canary.mjs
 *
 * OPEN-125 / Issue #87 -- credential-free diagnostic canary. Proves three things in
 * isolation from the real auth flow, so a sandbox/runtime problem can never be confused
 * with a login/credential problem:
 *
 *   1. the pinned `playwright` package (auth/node_modules, exact version from
 *      package.json) resolves and loads at all under whatever identity/sandbox invokes
 *      this script;
 *   2. PLAYWRIGHT_BROWSERS_PATH is visible in the process environment;
 *   3. plain `chromium.launch()` (no channel -- the same resolution used by
 *      playwright-login.mjs and by the generated MCP server config) succeeds, and a page
 *      can be opened and closed cleanly.
 *
 * Never reads a credential, never accepts one, never touches the network, never navigates
 * to any real site, never writes a storageState. Safe to run under the exact sandbox
 * properties of browser-qa-refresh@.service with no credentials directory bound at all.
 *
 * Two independent status lines, matching the diagnostic fields tracked on Issue #87:
 *   SYSTEMD_SANDBOX_CAN_SEE_BROWSER=PASS|FAIL   -- chromium.launch() itself succeeded
 *   CHROMIUM_LAUNCH_UNDER_SERVICE_SANDBOX=PASS|FAIL -- a page could be opened/closed too
 *
 * Unlike playwright-login.mjs, this script's errors can never contain a credential value
 * (there is none in scope) -- so, deliberately unlike playwright-login.mjs, it prints the
 * full err.message on failure. That is what makes it useful for diagnosing an opaque
 * PlaywrightError seen from the real auth flow.
 */

import { chromium } from "playwright";

async function main() {
  console.log(`BROWSERS_PATH_ENV=${process.env.PLAYWRIGHT_BROWSERS_PATH || "(unset)"}`);

  let browser;
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  } catch (err) {
    console.log("SYSTEMD_SANDBOX_CAN_SEE_BROWSER=FAIL");
    console.log(`LAUNCH_ERROR_NAME=${err.constructor.name}`);
    console.log(`LAUNCH_ERROR_MESSAGE=${err.message}`);
    process.exitCode = 1;
    return;
  }
  console.log("SYSTEMD_SANDBOX_CAN_SEE_BROWSER=PASS");

  try {
    const page = await browser.newPage();
    await page.close();
    console.log("CHROMIUM_LAUNCH_UNDER_SERVICE_SANDBOX=PASS");
  } catch (err) {
    console.log("CHROMIUM_LAUNCH_UNDER_SERVICE_SANDBOX=FAIL");
    console.log(`PAGE_ERROR_NAME=${err.constructor.name}`);
    console.log(`PAGE_ERROR_MESSAGE=${err.message}`);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.log("SYSTEMD_SANDBOX_CAN_SEE_BROWSER=FAIL");
  console.log(`UNEXPECTED_ERROR_NAME=${err.constructor.name}`);
  console.log(`UNEXPECTED_ERROR_MESSAGE=${err.message}`);
  process.exitCode = 1;
});
