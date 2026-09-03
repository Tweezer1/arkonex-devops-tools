#!/usr/bin/env node
/**
 * playwright-login.mjs --site-url <url> --output <path>
 *
 * OPEN-125 / Issue #87 -- performs a real Frappe Desk login and writes the resulting
 * Playwright storageState to --output. Invoked ONLY by refresh-persona.sh, running as
 * the dedicated OS user `browserqa-refresh`. NEVER invoked by Claude, NEVER invoked
 * inside a Claude Code session -- identical rule to the pre-existing
 * sites/deverp.arkonex.ca/private/e2e-playwright/auth-setup.mjs, which this design
 * generalizes to N personas instead of one human running it by hand per account.
 *
 * Credentials are read exclusively from the process environment
 * (BROWSER_QA_USER_ID / BROWSER_QA_PASSWORD, sourced by refresh-persona.sh from the
 * persona's credentials file). Never accepted as CLI arguments (visible via `ps`),
 * never logged, never printed, never written anywhere except into the browser's own
 * form fields.
 *
 * Frappe login behaviour (read from frappe/templates/includes/login/login.js, same
 * reference already used by auth-setup.mjs):
 *  - success -> full navigation (window.location.href) -> "load" event.
 *  - failure -> no navigation, an error message is shown, the button resets. This
 *    script treats "no navigation within the timeout" as FAIL, never as success.
 *
 * NOT EXECUTED IN PHASE A (OPEN-125). No browser installed, no credentials directory
 * exists yet. Source-only in this pass.
 */

import { chromium } from "playwright";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, "");
    out[key] = argv[i + 1];
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const siteUrl = args["site-url"];
  const outputPath = args["output"];
  if (!siteUrl || !outputPath) {
    console.error("playwright-login: --site-url and --output are required");
    process.exitCode = 2;
    return;
  }

  const userId = process.env.BROWSER_QA_USER_ID;
  const password = process.env.BROWSER_QA_PASSWORD;
  if (!userId || !password) {
    // Deliberately no value echoed -- only a statement that they are missing.
    console.error("playwright-login: BROWSER_QA_USER_ID/BROWSER_QA_PASSWORD not set in environment");
    process.exitCode = 1;
    return;
  }

  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext();
  const page = await context.newPage();

  try {
    await page.goto(`${siteUrl}/login`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.fill("#login_email", userId);
    await page.fill("#login_password", password);

    const navigated = page
      .waitForNavigation({ waitUntil: "load", timeout: 20000 })
      .then(() => true)
      .catch(() => false);

    await page.click("button.btn-login");
    const success = await navigated;

    if (!success) {
      console.error("playwright-login: no post-login navigation observed within timeout (treated as FAIL, never as success)");
      process.exitCode = 1;
      return;
    }

    await context.storageState({ path: outputPath });
    console.log("playwright-login: candidate storageState written");
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch((err) => {
  console.error(`playwright-login: unexpected error (${err.constructor.name})`);
  process.exitCode = 1;
});
