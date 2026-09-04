#!/usr/bin/env node
/**
 * validate-storage-state.mjs --site-url <url> --storage-state <path>
 *
 * OPEN-125 / Issue #87 -- proves a storageState corresponds to a REAL authenticated
 * Frappe session, not merely a file that exists with a plausible-looking date. Makes a
 * single lightweight authenticated request (frappe.auth.get_logged_user) using the
 * session cookie found in the storageState file, and prints ONLY the resulting identity
 * string to stdout ("Guest" if unauthenticated/expired, or the real user_id if valid).
 *
 * The cookie value itself is read into memory to build the request, but is NEVER
 * printed, logged, or written anywhere else -- only the resulting *identity* (a
 * username, not a secret) is emitted. Both refresh-persona.sh and verify-browser-qa.sh
 * consume this identity to produce AUTH_<persona>=PASS/EXPIRED/FAIL -- they never see
 * the cookie.
 *
 * NOT EXECUTED IN PHASE A (OPEN-125). No storageState exists yet on this instance.
 * Source-only in this pass.
 */

import { readFile } from "node:fs/promises";

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
  const storageStatePath = args["storage-state"];
  if (!siteUrl || !storageStatePath) {
    console.error("validate-storage-state: --site-url and --storage-state are required");
    process.exitCode = 2;
    return;
  }

  let state;
  try {
    state = JSON.parse(await readFile(storageStatePath, "utf-8"));
  } catch {
    // File missing/unreadable/corrupt -- treated identically to "no session".
    console.log("Guest");
    return;
  }

  const sidCookie = (state.cookies || []).find((c) => c.name === "sid");
  if (!sidCookie || !sidCookie.value) {
    console.log("Guest");
    return;
  }

  let response;
  try {
    response = await fetch(`${siteUrl}/api/method/frappe.auth.get_logged_user`, {
      headers: { Cookie: `sid=${sidCookie.value}` },
      redirect: "manual",
    });
  } catch {
    // Network/technical failure is distinct from "Guest" -- surfaced as empty output so
    // the caller (refresh-persona.sh) maps it to AUTH_<persona>=FAIL, not EXPIRED.
    return;
  }

  if (response.status !== 200) {
    console.log("Guest");
    return;
  }

  let body;
  try {
    body = await response.json();
  } catch {
    return;
  }

  console.log(body.message || "Guest");
}

main();
