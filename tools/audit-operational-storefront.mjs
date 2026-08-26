#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const DEFAULT_CHROME =
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function parseArgs(argv) {
  const args = {
    chrome: DEFAULT_CHROME,
    baseUrl: "http://127.0.0.1:9292",
    port: 9380,
    connectToExistingChrome: false,
    report: ".tmp\\phase7-operational-storefront-20260805.json",
    product: "the-linen-summer-shirt-navy",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--chrome") args.chrome = value;
    else if (key === "--base-url") args.baseUrl = value;
    else if (key === "--port") args.port = Number(value);
    else if (key === "--connect-port") {
      args.port = Number(value);
      args.connectToExistingChrome = true;
    } else if (key === "--report") args.report = value;
    else if (key === "--product") args.product = value;
    else continue;
    index += 1;
  }
  return args;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForTarget(port, attempts = 80) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) {
        const targets = await response.json();
        const target = targets.find((item) => item.type === "page");
        if (target?.webSocketDebuggerUrl) return target;
      }
    } catch {
      // Chrome is still starting.
    }
    await sleep(250);
  }
  throw new Error("Chrome DevTools target did not become available.");
}

function createCdpClient(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  const listeners = new Map();
  let commandId = 0;
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id) {
      for (const listener of listeners.get(message.method) || []) {
        listener(message.params || {});
      }
      return;
    }
    if (!pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result || {});
  });
  return {
    async send(method, params = {}) {
      await opened;
      commandId += 1;
      const id = commandId;
      const result = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
      socket.send(JSON.stringify({ id, method, params }));
      return result;
    },
    on(method, listener) {
      const current = listeners.get(method) || [];
      current.push(listener);
      listeners.set(method, current);
    },
    close() {
      socket.close();
    },
  };
}

async function evaluate(client, expression) {
  const result = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result?.value;
}

async function waitForCondition(client, expression, message, attempts = 80) {
  let lastValue;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    lastValue = await evaluate(client, expression);
    if (lastValue) return lastValue;
    await sleep(250);
  }
  throw new Error(`${message}: ${JSON.stringify(lastValue)}`);
}

async function navigate(client, url) {
  await client.send("Page.navigate", { url });
  await waitForCondition(
    client,
    `document.readyState === 'complete' && location.href.startsWith(${JSON.stringify(url)})`,
    `Document did not reach ${url}`,
  );
  await sleep(500);
}

async function setViewport(client, width, height, mobile = false) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
  });
  await client.send("Emulation.setTouchEmulationEnabled", { enabled: mobile });
}

function classifyTrackingRequest(request) {
  const url = request.url || "";
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const hostname = parsed.hostname.toLowerCase();
  const pathname = parsed.pathname.toLowerCase();
  let provider = "";
  let eventName = "";
  let destination = "";
  let marketing = false;
  if (hostname.includes("google-analytics.com") && pathname.includes("collect")) {
    provider = "google_analytics";
    eventName = parsed.searchParams.get("en") || parsed.searchParams.get("t") || "collect";
    destination = parsed.searchParams.get("tid") || "";
    marketing = true;
  } else if (hostname.includes("googletagmanager.com")) {
    provider = "google_tag_manager";
    eventName = "script";
    destination = parsed.searchParams.get("id") || "";
    marketing = true;
  } else if (hostname.includes("facebook.com") && pathname.includes("/tr")) {
    provider = "meta";
    eventName = parsed.searchParams.get("ev") || "request";
    destination = parsed.searchParams.get("id") || "";
    marketing = true;
  } else if (hostname.includes("connect.facebook.net")) {
    provider = "meta";
    eventName = "script";
    marketing = true;
  } else if (hostname.includes("tiktok.com")) {
    provider = "tiktok";
    eventName = parsed.searchParams.get("event") || "request";
    marketing = true;
  } else if (hostname.includes("pinterest.com")) {
    provider = "pinterest";
    eventName = parsed.searchParams.get("event") || "request";
    marketing = true;
  } else if (hostname.includes("snapchat.com")) {
    provider = "snapchat";
    eventName = parsed.searchParams.get("event") || "request";
    marketing = true;
  } else if (
    hostname.includes("monorail-edge.shopifysvc.com") ||
    pathname.includes("trekkie") ||
    pathname.includes("shopify-analytics")
  ) {
    provider = "shopify_analytics";
    eventName = "request";
  } else {
    return null;
  }
  return {
    provider,
    eventName,
    destination,
    marketing,
    method: request.method,
    url: `${parsed.origin}${parsed.pathname}`,
    postData: (request.postData || "").slice(0, 1000),
  };
}

async function privacyState(client) {
  return evaluate(
    client,
    `(() => {
      const api = window.Shopify?.customerPrivacy;
      const buttonTexts = [...document.querySelectorAll('button')]
        .filter((button) => {
          const style = getComputedStyle(button);
          const rect = button.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        })
        .map((button) => button.textContent.replace(/\\s+/g, ' ').trim())
        .filter(Boolean);
      if (!api) return { apiPresent: false, buttonTexts };
      const call = (name) => {
        try {
          return typeof api[name] === 'function' ? api[name]() : null;
        } catch (error) {
          return { error: error.message };
        }
      };
      return {
        apiPresent: true,
        currentVisitorConsent: call('currentVisitorConsent'),
        analyticsProcessingAllowed: call('analyticsProcessingAllowed'),
        marketingAllowed: call('marketingAllowed'),
        preferencesProcessingAllowed: call('preferencesProcessingAllowed'),
        saleOfDataAllowed: call('saleOfDataAllowed'),
        shouldShowBanner: call('shouldShowBanner'),
        buttonTexts
      };
    })()`,
  );
}

async function waitForPrivacyUi(client, attempts = 40) {
  let state = await privacyState(client);
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const hasDecisionButton = state.buttonTexts.some((text) =>
      /^(accept|accept all|decline|decline all)$/i.test(text),
    );
    if (state.apiPresent || hasDecisionButton) return state;
    await sleep(250);
    state = await privacyState(client);
  }
  return state;
}

async function readConsentLayout(client) {
  return evaluate(
    client,
    `(() => {
      const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const buttons = [...document.querySelectorAll('button')]
        .filter((button) => /^(accept|accept all|decline|decline all|manage preferences)$/i.test(clean(button.textContent)))
        .map((button) => {
          const rect = button.getBoundingClientRect();
          return {
            text: clean(button.textContent),
            className: button.className,
            visible: rect.width > 0 && rect.height > 0,
            rect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width },
            parentClassName: button.parentElement?.className || ''
          };
        });
      return {
        innerWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body?.scrollWidth || 0,
        buttons,
        visibleDecisionButtonCount: buttons
          .filter((button) => button.visible)
          .filter((button) => /^(accept|accept all|decline|decline all)$/i.test(button.text)).length,
        decisionButtonsFit: buttons
          .filter((button) => button.visible)
          .filter((button) => /^(accept|accept all|decline|decline all)$/i.test(button.text))
          .every((button) => button.rect.left >= 0 && button.rect.right <= innerWidth)
      };
    })()`,
  );
}

async function waitForConsentLayout(client, attempts = 40) {
  let layout = await readConsentLayout(client);
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (layout.visibleDecisionButtonCount) return layout;
    await sleep(250);
    layout = await readConsentLayout(client);
  }
  return layout;
}

async function clickConsent(client, intent) {
  const words = intent === "accept" ? ["accept all", "accept"] : ["decline all", "decline"];
  const clicked = await evaluate(
    client,
    `(() => {
      const words = ${JSON.stringify(words)};
      const buttons = [...document.querySelectorAll('button')];
      const button = buttons.find((item) => {
        const text = item.textContent.replace(/\\s+/g, ' ').trim().toLowerCase();
        const rect = item.getBoundingClientRect();
        const style = getComputedStyle(item);
        return words.includes(text) && rect.width > 0 && rect.height > 0 &&
          style.display !== 'none' && style.visibility !== 'hidden';
      });
      if (!button) return false;
      button.click();
      return true;
    })()`,
  );
  if (clicked) await sleep(1000);
  return clicked;
}

async function showConsentPreferences(client) {
  const clicked = await evaluate(
    client,
    `(() => {
      const link = document.querySelector('a[href*="shopifyReshowConsentBanner"]');
      if (!link) return false;
      link.click();
      return true;
    })()`,
  );
  if (clicked) await sleep(500);
  return clicked;
}

async function clearVisitorData(client, origin) {
  await client.send("Network.clearBrowserCookies");
  await client.send("Storage.clearDataForOrigin", {
    origin,
    storageTypes: "all",
  });
}

async function readPage(client, baseUrl, pagePath) {
  const url = `${baseUrl}${pagePath}`;
  await navigate(client, url);
  return evaluate(
    client,
    `(() => {
      const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const footerLinks = [...document.querySelectorAll('footer a')].map((link) => ({
        text: clean(link.textContent),
        href: link.href
      }));
      return {
        path: location.pathname,
        url: location.href,
        title: document.title,
        mainText: clean(document.querySelector('main')?.textContent).slice(0, 6000),
        bodyText: clean(document.body?.textContent).slice(0, 10000),
        mainPresent: Boolean(document.querySelector('main')),
        footerPresent: Boolean(document.querySelector('footer')),
        footerLinks,
        emails: [...document.querySelectorAll('a[href^="mailto:"]')].map((link) => link.href),
        phones: [...document.querySelectorAll('a[href^="tel:"]')].map((link) => link.href),
      };
    })()`,
  );
}

async function checkFooterLinks(client, baseUrl) {
  return evaluate(
    client,
    `(async () => {
      const links = [...new Set([...document.querySelectorAll('footer a[href]')]
        .map((link) => link.href)
        .filter((href) => href.startsWith(${JSON.stringify(baseUrl)}))
        .filter((href) => !href.includes('#shopifyReshowConsentBanner')))]
        .slice(0, 60);
      const results = [];
      for (const href of links) {
        try {
          const response = await fetch(href, { headers: { Accept: 'text/html' } });
          results.push({ href, status: response.status, ok: response.ok });
        } catch (error) {
          results.push({ href, status: null, ok: false, error: error.message });
        }
      }
      return results;
    })()`,
  );
}

async function addProduct(client, baseUrl, productHandle) {
  await navigate(client, `${baseUrl}/products/${productHandle}`);
  const before = await evaluate(
    client,
    `fetch('/cart.js').then((response) => response.json()).then((cart) => cart.item_count)`,
  );
  const clicked = await evaluate(
    client,
    `(() => {
      const button = document.querySelector('.product-form__submit:not([disabled])');
      if (!button || button.getAttribute('aria-disabled') === 'true') return false;
      button.click();
      return true;
    })()`,
  );
  let after = before;
  if (clicked) {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await sleep(250);
      after = await evaluate(
        client,
        `fetch('/cart.js').then((response) => response.json()).then((cart) => cart.item_count)`,
      );
      if (after > before) break;
    }
  }
  await sleep(1000);
  return { clicked, before, after, added: after > before };
}

function duplicateEvents(events) {
  const counts = new Map();
  for (const event of events) {
    if (event.eventName === "script" || event.eventName === "request") continue;
    const key = [event.phase, event.provider, event.destination, event.eventName].join(":");
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .filter(([, count]) => count > 1)
    .map(([signature, count]) => ({ signature, count }));
}

function makeCheck(key, status, detail, evidence) {
  return { key, status, detail, ...(evidence === undefined ? {} : { evidence }) };
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const origin = new URL(baseUrl).origin;
  const reportPath = path.resolve(args.report);
  const profilePath = path.resolve(`.tmp\\chrome-operational-storefront-${process.pid}`);
  await mkdir(path.dirname(reportPath), { recursive: true });
  if (!args.connectToExistingChrome) await mkdir(profilePath, { recursive: true });

  let chrome;
  if (!args.connectToExistingChrome) {
    chrome = spawn(
      args.chrome,
      [
        "--headless=new",
        "--disable-gpu",
        "--window-size=1440,900",
        `--remote-debugging-port=${args.port}`,
        `--user-data-dir=${profilePath}`,
        "about:blank",
      ],
      { stdio: "ignore", windowsHide: true },
    );
  }

  let client;
  try {
    const target = await waitForTarget(args.port);
    client = createCdpClient(target.webSocketDebuggerUrl);
    const requests = [];
    const responses = [];
    const consoleMessages = [];
    let phase = "setup";
    client.on("Network.requestWillBeSent", (event) => {
      const tracking = classifyTrackingRequest(event.request || {});
      if (tracking) requests.push({ phase, timestamp: event.timestamp, ...tracking });
    });
    client.on("Network.responseReceived", (event) => {
      if (event.type === "Document") {
        responses.push({ phase, url: event.response.url, status: event.response.status });
      }
    });
    client.on("Runtime.exceptionThrown", (event) => {
      consoleMessages.push(
        event.exceptionDetails?.exception?.description || event.exceptionDetails?.text || "Unknown exception",
      );
    });
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Network.enable");
    await clearVisitorData(client, origin);

    await setViewport(client, 390, 844, true);
    phase = "mobile_initial_consent_layout";
    await navigate(client, `${baseUrl}/?_qa=mobile-initial-consent`);
    const initialMobilePrivacy = await waitForPrivacyUi(client);
    const initialMobileConsentLayout = await waitForConsentLayout(client);

    await clearVisitorData(client, origin);
    await setViewport(client, 1440, 900);

    phase = "before_consent";
    await navigate(client, `${baseUrl}/`);
    const privacyBefore = await waitForPrivacyUi(client);
    const beforeConsentRequests = requests.filter((item) => item.phase === phase);

    phase = "after_decline";
    const declined = await clickConsent(client, "decline");
    await navigate(client, `${baseUrl}/?_qa=declined`);
    const privacyDeclined = await waitForPrivacyUi(client);
    const declinedRequests = requests.filter((item) => item.phase === phase);

    phase = "before_accept";
    await navigate(client, `${baseUrl}/?_qa=accept`);
    const preferencesReopenedForAccept = await showConsentPreferences(client);
    await waitForPrivacyUi(client);
    const accepted = await clickConsent(client, "accept");
    phase = "after_accept";
    await navigate(client, `${baseUrl}/?_qa=accepted`);
    const privacyAccepted = await waitForPrivacyUi(client);

    phase = "add_to_cart";
    const addToCart = await addProduct(client, baseUrl, args.product);

    await setViewport(client, 390, 844, true);
    phase = "mobile_consent_layout";
    await navigate(client, `${baseUrl}/?_qa=mobile-consent`);
    const preferencesReopenedForMobile = await showConsentPreferences(client);
    await waitForPrivacyUi(client);
    const mobileConsentLayout = await waitForConsentLayout(client);
    const declinedFromMobilePreferences = await clickConsent(client, "decline");
    phase = "after_mobile_decline";
    await navigate(client, `${baseUrl}/?_qa=mobile-declined`);
    const marketingAfterMobileDecline = requests.filter(
      (item) => item.phase === phase && item.marketing,
    );
    await setViewport(client, 1440, 900);

    phase = "content_review";
    const pagePaths = [
      "/",
      "/pages/brickstore",
      "/policies/contact-information",
      "/policies/legal-notice",
      "/policies/privacy-policy",
      "/policies/refund-policy",
      "/policies/shipping-policy",
      "/policies/terms-of-service",
    ];
    const pages = [];
    for (const pagePath of pagePaths) pages.push(await readPage(client, baseUrl, pagePath));
    const footerLinks = await checkFooterLinks(client, baseUrl);

    const marketingBefore = beforeConsentRequests.filter((item) => item.marketing);
    const marketingDeclined = declinedRequests.filter((item) => item.marketing);
    const marketingAfterAccept = requests.filter(
      (item) => item.phase === "after_accept" && item.marketing,
    );
    const addToCartEvents = requests.filter(
      (item) => item.phase === "add_to_cart" && /add.?to.?cart/i.test(item.eventName + item.postData),
    );
    const duplicates = duplicateEvents(requests);
    const policyPages = pages.filter((page) => page.path.startsWith("/policies/"));
    const brickstore = pages.find((page) => page.path === "/pages/brickstore");
    const brickText = brickstore?.bodyText || "";
    const hasAddress = /rijnstraat\s*14/i.test(brickText) && /arnhem/i.test(brickText);
    const hasContact = /info@nbharnhem\.com/i.test(brickText) || (brickstore?.emails.length || 0) > 0;
    const hasHours = /(monday|tuesday|wednesday|thursday|friday|saturday|sunday|maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)/i.test(brickText);
    const brokenFooterLinks = footerLinks.filter((item) => !item.ok);

    const checks = [
      makeCheck(
        "footer_links",
        footerLinks.length && !brokenFooterLinks.length ? "pass" : "fail",
        footerLinks.length && !brokenFooterLinks.length
          ? "All internal footer links returned a successful response."
          : "One or more internal footer links failed.",
        { checked: footerLinks.length, broken: brokenFooterLinks },
      ),
      makeCheck(
        "policy_pages",
        policyPages.length === 6 && policyPages.every((page) => page.mainPresent && page.mainText.length > 100)
          ? "pass"
          : "fail",
        "All six policy pages render substantive main content.",
        { pages: policyPages.map((page) => ({ path: page.path, characters: page.mainText.length })) },
      ),
      makeCheck(
        "contact_details",
        hasAddress && hasContact ? "pass" : "fail",
        hasAddress && hasContact
          ? "The brick-store page contains the Arnhem address and contact route."
          : "The brick-store page is missing the expected address or contact route.",
      ),
      makeCheck(
        "store_hours",
        hasHours ? "pass" : "fail",
        hasHours
          ? "Store hours are present on the brick-store page."
          : "No store hours were detected on the brick-store page.",
      ),
      makeCheck(
        "consent_gating",
        (declined || declinedFromMobilePreferences) &&
          !marketingBefore.length &&
          !marketingDeclined.length &&
          !marketingAfterMobileDecline.length
          ? "pass"
          : "warn",
        (declined || declinedFromMobilePreferences) &&
          !marketingBefore.length &&
          !marketingDeclined.length &&
          !marketingAfterMobileDecline.length
          ? "The consent banner declined successfully and no marketing requests fired before consent or after decline."
          : "Consent gating could not be fully confirmed.",
        {
          declined,
          declinedFromMobilePreferences,
          accepted,
          preferencesReopenedForAccept,
          preferencesReopenedForMobile,
          marketingBefore: marketingBefore.length,
          marketingAfterDecline: marketingDeclined.length,
          marketingAfterMobileDecline: marketingAfterMobileDecline.length,
          privacyBefore,
          privacyDeclined,
          privacyAccepted,
        },
      ),
      makeCheck(
        "mobile_consent_layout",
        initialMobileConsentLayout.visibleDecisionButtonCount &&
          initialMobileConsentLayout.decisionButtonsFit &&
          mobileConsentLayout.visibleDecisionButtonCount &&
          mobileConsentLayout.decisionButtonsFit
          ? "pass"
          : "fail",
        initialMobileConsentLayout.visibleDecisionButtonCount &&
          initialMobileConsentLayout.decisionButtonsFit &&
          mobileConsentLayout.visibleDecisionButtonCount &&
          mobileConsentLayout.decisionButtonsFit
          ? "Initial consent and preference controls fit within a 390px mobile viewport."
          : "Initial consent or preference controls overflow or were not found in a 390px mobile viewport.",
        {
          initialPrivacy: initialMobilePrivacy,
          initialBanner: initialMobileConsentLayout,
          preferences: mobileConsentLayout,
        },
      ),
      makeCheck(
        "analytics_after_consent",
        marketingAfterAccept.length ? "pass" : "warn",
        marketingAfterAccept.length
          ? "Marketing analytics requests fired after consent."
          : "No third-party marketing analytics request was observed after consent.",
        { requests: marketingAfterAccept },
      ),
      makeCheck(
        "duplicate_events",
        duplicates.length ? "fail" : "pass",
        duplicates.length
          ? "Potential duplicate analytics events were observed."
          : "No exact duplicate named analytics events were observed.",
        { duplicates },
      ),
      makeCheck(
        "add_to_cart_conversion",
        addToCart.added && addToCartEvents.length === 1 ? "pass" : "warn",
        addToCart.added && addToCartEvents.length === 1
          ? "Add-to-cart completed and emitted one matching conversion event."
          : "Add-to-cart or its analytics event could not be fully confirmed.",
        { cart: addToCart, matchingEvents: addToCartEvents },
      ),
    ];
    const summary = {
      checks: checks.length,
      passed: checks.filter((item) => item.status === "pass").length,
      warnings: checks.filter((item) => item.status === "warn").length,
      errors: checks.filter((item) => item.status === "fail").length,
      trackingRequests: requests.length,
    };
    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "read_only_operational_storefront_audit",
      baseUrl,
      product: args.product,
      summary,
      checks,
      pages,
      footerLinks,
      trackingRequests: requests,
      documentResponses: responses,
      consoleMessages,
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\nReport: ${reportPath}\n`);
    process.exitCode = summary.errors ? 1 : 0;
  } finally {
    client?.close();
    if (chrome && !chrome.killed) chrome.kill();
    if (!args.connectToExistingChrome) {
      await sleep(500);
      await rm(profilePath, { recursive: true, force: true, maxRetries: 5, retryDelay: 250 });
    }
  }
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
