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
    port: 9350,
    report: ".tmp\\phase4-theme-flows-20260729.json",
    desktopScreenshot: ".tmp\\phase4-theme-flows-desktop-20260729.png",
    mobileScreenshot: ".tmp\\phase4-theme-flows-mobile-20260729.png",
    regularProduct: "thuy-t-shirt-aran-creme",
    contactProduct: "beira-solid-black-dark-grey",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--chrome") args.chrome = value;
    else if (key === "--base-url") args.baseUrl = value;
    else if (key === "--port") args.port = Number(value);
    else if (key === "--report") args.report = value;
    else if (key === "--desktop-screenshot") args.desktopScreenshot = value;
    else if (key === "--mobile-screenshot") args.mobileScreenshot = value;
    else if (key === "--regular-product") args.regularProduct = value;
    else if (key === "--contact-product") args.contactProduct = value;
    else continue;
    index += 1;
  }
  return args;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForTarget(port, url, attempts = 80) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      if (response.ok) {
        const targets = await response.json();
        const target =
          targets.find((item) => item.type === "page" && item.url === url) ||
          targets.find((item) => item.type === "page");
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
      const result = new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
      });
      socket.send(JSON.stringify({ id, method, params }));
      return result;
    },
    close() {
      socket.close();
    },
    on(method, listener) {
      const current = listeners.get(method) || [];
      current.push(listener);
      listeners.set(method, current);
    },
  };
}

async function evaluate(client, expression) {
  const result = await client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
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

async function waitForDocument(client, expectedPrefix) {
  return waitForCondition(
    client,
    `(() => ({
      ready: document.readyState === 'complete',
      url: window.location.href
    }))()`,
    "Document did not finish loading",
  ).then(async () => {
    await waitForCondition(
      client,
      `document.readyState === 'complete' && window.location.href.startsWith(${JSON.stringify(expectedPrefix)})`,
      `Document did not reach ${expectedPrefix}`,
    );
  });
}

async function navigate(client, url) {
  await client.send("Page.navigate", { url });
  await waitForDocument(client, url);
  await dismissCookieBanner(client);
}

async function setViewport(client, width, height, mobile = false) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
  });
}

function result(key, label, status, details = {}) {
  return { key, label, status, details };
}

function visibleHelper() {
  return `
    const isVisible = (selectorOrElement) => {
      const element = typeof selectorOrElement === 'string'
        ? document.querySelector(selectorOrElement)
        : selectorOrElement;
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return rect.width > 0 &&
        rect.height > 0 &&
        style.display !== 'none' &&
        style.visibility !== 'hidden' &&
        style.opacity !== '0';
    };
  `;
}

async function readHeaderState(client) {
  return evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const navLinks = [
        ...document.querySelectorAll('.header__inline-menu a, header-drawer a')
      ].filter(isVisible);
      const account = document.querySelector('.header__icon--account');
      return {
        headerVisible: isVisible('header.header'),
        logoVisible: isVisible('.header__heading-link'),
        navLinkCount: navLinks.length,
        menuVisible: isVisible('#Details-menu-drawer-container > summary'),
        searchVisible: [...document.querySelectorAll('details-modal.header__search summary')].some(isVisible),
        cartVisible: isVisible('#cart-icon-bubble'),
        accountPresent: Boolean(account),
        accountVisible: account ? isVisible(account) : false,
        accountHref: account?.href || '',
      };
    })()`,
  );
}

async function dismissCookieBanner(client) {
  const clicked = await evaluate(
    client,
    `(() => {
      const buttons = [...document.querySelectorAll('button')];
      const button = buttons.find((item) =>
        item.textContent.replace(/\\s+/g, ' ').trim().toLowerCase() === 'decline'
      );
      if (!button) return false;
      button.click();
      return true;
    })()`,
  );
  if (clicked) await sleep(250);
}

async function runSearchFlow(client, baseUrl) {
  await navigate(client, `${baseUrl}/collections/drakes`);
  const opened = await evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const summary = [...document.querySelectorAll('details-modal.header__search summary')]
        .find(isVisible);
      if (!summary) return false;
      summary.click();
      return true;
    })()`,
  );
  if (!opened) {
    return result("search_submit", "Search", "fail", {
      error: "No visible search summary was found.",
    });
  }
  await waitForCondition(
    client,
    "Boolean(document.querySelector('details-modal.header__search details[open]'))",
    "Search modal did not open",
  );
  const submitted = await evaluate(
    client,
    `(() => {
      const details = document.querySelector('details-modal.header__search details[open]');
      const input = details?.querySelector('input[type="search"]');
      const form = details?.querySelector('form[role="search"]');
      if (!input || !form) return false;
      input.value = 'drake';
      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'drake' }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      form.requestSubmit();
      return true;
    })()`,
  );
  if (!submitted) {
    return result("search_submit", "Search", "fail", {
      error: "Search form was not available after opening the modal.",
    });
  }
  await waitForDocument(client, `${baseUrl}/search`);
  const state = await evaluate(
    client,
    `(() => {
      const params = new URLSearchParams(location.search);
      return {
        path: location.pathname,
        query: params.get('q') || '',
        cardCount: document.querySelectorAll('#product-grid .grid__item, .product-card-wrapper').length,
        resultText: document.body.textContent.replace(/\\s+/g, ' ').trim().slice(0, 300)
      };
    })()`,
  );
  const passed =
    state.path.includes("/search") &&
    state.query.toLowerCase() === "drake" &&
    state.cardCount > 0;
  return result("search_submit", "Search", passed ? "pass" : "fail", state);
}

async function runCartFlow(client, baseUrl) {
  await navigate(client, `${baseUrl}/collections/drakes`);
  const clicked = await evaluate(
    client,
    `(() => {
      const cart = document.querySelector('#cart-icon-bubble');
      if (!cart) return false;
      cart.click();
      return true;
    })()`,
  );
  if (!clicked) {
    return result("cart_drawer", "Cart drawer", "fail", {
      error: "Cart icon was not found.",
    });
  }
  await waitForCondition(
    client,
    "Boolean(document.querySelector('cart-drawer.active'))",
    "Cart drawer did not open",
  );
  await sleep(800);
  const state = await evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const drawer = document.querySelector('cart-drawer');
      return {
        drawerPresent: Boolean(drawer),
        drawerActive: drawer?.classList.contains('active') || false,
        dialogVisible: isVisible('#CartDrawer .drawer__inner'),
        emptyTextVisible: isVisible('.cart__empty-text'),
        closeButtonVisible: isVisible('cart-drawer .drawer__close'),
        continueShoppingVisible: isVisible('.cart-drawer__empty-content a.button'),
      };
    })()`,
  );
  const passed = state.drawerPresent && state.drawerActive && state.dialogVisible;
  return result("cart_drawer", "Cart drawer", passed ? "pass" : "fail", state);
}

async function runAccountFlow(client, baseUrl) {
  await navigate(client, `${baseUrl}/collections/drakes`);
  const accountHref = await evaluate(
    client,
    "document.querySelector('.header__icon--account')?.href || ''",
  );
  if (!accountHref) {
    return result("account_link", "Account", "not_applicable", {
      note: "Customer account link is not rendered in the header.",
    });
  }
  const accountUrl = new URL(accountHref);
  const localUrl = new URL(baseUrl);
  if (accountUrl.origin !== localUrl.origin) {
    const externalState = {
      url: accountHref,
      path: accountUrl.pathname,
      externalAccountHandoff: true,
    };
    const passed = accountUrl.pathname.includes(
      "/customer_authentication/redirect",
    );
    return result(
      "account_link",
      "Account",
      passed ? "pass" : "fail",
      externalState,
    );
  }
  await navigate(client, accountHref);
  const state = await evaluate(
    client,
    `(() => ({
      url: location.href,
      path: location.pathname,
      title: document.title,
      hasLoginForm: Boolean(
        document.querySelector('form[action*="/account/login"], #customer_login, input[name="customer[email]"]')
      ),
      hasAccountCopy: /login|account|sign in/i.test(document.body.textContent || ''),
    }))()`,
  );
  const passed = state.path.includes("/account") && (
    state.hasLoginForm || state.hasAccountCopy
  );
  return result("account_link", "Account", passed ? "pass" : "fail", state);
}

async function runMobileMenuFlow(client, baseUrl) {
  await setViewport(client, 390, 844, true);
  await navigate(client, `${baseUrl}/collections/drakes`);
  const clicked = await evaluate(
    client,
    `(() => {
      const summary = document.querySelector('#Details-menu-drawer-container > summary');
      if (!summary) return false;
      summary.click();
      return true;
    })()`,
  );
  if (!clicked) {
    return result("mobile_menu", "Mobile menu", "fail", {
      error: "Mobile menu summary was not found.",
    });
  }
  await waitForCondition(
    client,
    "document.querySelector('#Details-menu-drawer-container')?.hasAttribute('open')",
    "Mobile menu did not open",
  );
  await sleep(500);
  const state = await evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const drawer = document.querySelector('#menu-drawer');
      const links = [...document.querySelectorAll('#menu-drawer a')]
        .filter(isVisible)
        .map((link) => ({
          text: link.textContent.replace(/\\s+/g, ' ').trim(),
          href: link.href
        }));
      return {
        drawerVisible: isVisible(drawer),
        linkCount: links.length,
        hasInStoreExclusiveLink: links.some((link) =>
          link.href.includes('/collections/in-store-exclusive') ||
          link.text.toLowerCase().includes('in-store exclusive')
        ),
        hasAccountLink: Boolean(document.querySelector('.menu-drawer__account')),
        firstLinks: links.slice(0, 8),
      };
    })()`,
  );
  const passed =
    state.drawerVisible &&
    state.linkCount > 0 &&
    state.hasInStoreExclusiveLink;
  return result("mobile_menu", "Mobile menu", passed ? "pass" : "fail", state);
}

async function readButtonMetrics(client) {
  return evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const readButton = (button) => {
        const rect = button.getBoundingClientRect();
        const style = getComputedStyle(button);
        const text = button.innerText.replace(/\\s+/g, ' ').trim();
        return {
          tag: button.tagName.toLowerCase(),
          text,
          classes: button.className || '',
          href: button.href || '',
          width: Math.round(rect.width * 10) / 10,
          height: Math.round(rect.height * 10) / 10,
          fontSize: style.fontSize,
          lineHeight: style.lineHeight,
          paddingTop: style.paddingTop,
          paddingBottom: style.paddingBottom,
          display: style.display,
          overflowX: button.scrollWidth > Math.ceil(rect.width) + 1,
          overflowY: button.scrollHeight > Math.ceil(rect.height) + 2,
          fullWidth: button.classList.contains('button--full-width'),
          primary: button.classList.contains('button--primary'),
          secondary: button.classList.contains('button--secondary'),
        };
      };
      const buttons = [
        ...document.querySelectorAll('a.button, button.button, .shopify-payment-button__button')
      ].filter((button) =>
        isVisible(button) &&
        !button.classList.contains('visually-hidden') &&
        !button.classList.contains('skip-to-content-link')
      ).map(readButton);
      const productSubmitElement = [
        ...document.querySelectorAll('.product-form__submit')
      ].find(isVisible);
      return {
        url: location.href,
        title: document.title,
        buttons,
        productSubmit: productSubmitElement ? readButton(productSubmitElement) : null,
      };
    })()`,
  );
}

function numericPixels(value) {
  const parsed = Number.parseFloat(String(value || "").replace("px", ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function compareButtons(regular, contact, viewport) {
  const issues = [];
  if (!regular) {
    issues.push(`${viewport}: regular product submit button is missing`);
  }
  if (!contact) {
    issues.push(`${viewport}: Contact us submit action is missing`);
  }
  if (!regular || !contact) return issues;
  if (!contact.text.toLowerCase().includes("contact us")) {
    issues.push(`${viewport}: Contact us text was '${contact.text}'`);
  }
  for (const className of [
    "product-form__submit",
    "button",
    "button--full-width",
  ]) {
    if (!contact.classes.includes(className)) {
      issues.push(`${viewport}: Contact us is missing .${className}`);
    }
  }
  if (Math.abs(regular.height - contact.height) > 2) {
    issues.push(
      `${viewport}: Contact us height ${contact.height}px differs from peer ${regular.height}px`,
    );
  }
  if (
    Math.abs(
      numericPixels(regular.fontSize) - numericPixels(contact.fontSize),
    ) > 0.5
  ) {
    issues.push(
      `${viewport}: Contact us font size ${contact.fontSize} differs from peer ${regular.fontSize}`,
    );
  }
  if (contact.overflowX || contact.overflowY) {
    issues.push(`${viewport}: Contact us text overflows its button`);
  }
  return issues;
}

async function runButtonSizingFlow(
  client,
  baseUrl,
  regularProduct,
  contactProduct,
) {
  const pages = [];
  const comparisons = [];
  const viewports = [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    { name: "mobile", width: 390, height: 844, mobile: true },
  ];
  for (const viewport of viewports) {
    await setViewport(client, viewport.width, viewport.height, viewport.mobile);
    await navigate(client, `${baseUrl}/products/${regularProduct}`);
    const regular = await readButtonMetrics(client);
    await navigate(client, `${baseUrl}/products/${contactProduct}`);
    const contact = await readButtonMetrics(client);
    pages.push({ viewport: viewport.name, kind: "regular", ...regular });
    pages.push({ viewport: viewport.name, kind: "contact", ...contact });
    comparisons.push({
      viewport: viewport.name,
      regularProductSubmit: regular.productSubmit,
      contactProductSubmit: contact.productSubmit,
      issues: compareButtons(
        regular.productSubmit,
        contact.productSubmit,
        viewport.name,
      ),
    });
  }
  const buttonOverflow = pages.flatMap((page) =>
    page.buttons
      .filter((button) => button.text && (button.overflowX || button.overflowY))
      .map((button) => ({
        viewport: page.viewport,
        kind: page.kind,
        url: page.url,
        text: button.text,
        width: button.width,
        height: button.height,
        overflowX: button.overflowX,
        overflowY: button.overflowY,
      })),
  );
  const shortButtons = pages.flatMap((page) =>
    page.buttons
      .filter((button) => button.text && button.height < 40)
      .map((button) => ({
        viewport: page.viewport,
        kind: page.kind,
        url: page.url,
        text: button.text,
        height: button.height,
      })),
  );
  const issues = [
    ...comparisons.flatMap((item) => item.issues),
    ...buttonOverflow.map(
      (item) =>
        `${item.viewport}: '${item.text}' overflows on ${item.kind} product page`,
    ),
    ...shortButtons.map(
      (item) =>
        `${item.viewport}: '${item.text}' button height is ${item.height}px`,
    ),
  ];
  return result(
    "button_sizing",
    "Button sizing",
    issues.length ? "fail" : "pass",
    {
      regularProduct,
      contactProduct,
      pages,
      comparisons,
      buttonOverflow,
      shortButtons,
      issues,
    },
  );
}

async function readLayoutMetrics(client) {
  return evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight;
      const productCards = [
        ...document.querySelectorAll('#product-grid > .grid__item')
      ].filter(isVisible).map((item, index) => {
        const rect = item.getBoundingClientRect();
        return {
          index,
          left: Math.round(rect.left),
          top: Math.round(rect.top),
          right: Math.round(rect.right),
          bottom: Math.round(rect.bottom),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      });
      const overlaps = [];
      for (let outer = 0; outer < productCards.length; outer += 1) {
        for (let inner = outer + 1; inner < productCards.length; inner += 1) {
          const a = productCards[outer];
          const b = productCards[inner];
          const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
          const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
          if (width > 4 && height > 4) {
            overlaps.push({ a: a.index, b: b.index, width, height });
          }
        }
      }
      const main = document.querySelector('main, #MainContent');
      const mainRect = main?.getBoundingClientRect();
      return {
        url: location.href,
        title: document.title,
        viewportWidth,
        viewportHeight,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
        horizontalOverflow: document.documentElement.scrollWidth > viewportWidth + 1,
        mainHeight: mainRect ? Math.round(mainRect.height) : 0,
        bodyTextLength: document.body.innerText.replace(/\\s+/g, ' ').trim().length,
        productCardCount: productCards.length,
        productGridOverlaps: overlaps,
      };
    })()`,
  );
}

async function runLayoutSmokeFlow(client, baseUrl) {
  const paths = [
    "/collections/drakes",
    "/collections/all?filter.p.m.custom.brand=Drake%27s",
    "/collections/a-kind-of-guise",
    "/collections/in-store-exclusive",
    "/products/thuy-t-shirt-aran-creme",
    "/products/beira-solid-black-dark-grey",
    "/search?q=drake&options%5Bprefix%5D=last",
    "/policies/contact-information",
  ];
  const viewports = [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    { name: "mobile", width: 390, height: 844, mobile: true },
  ];
  const pages = [];
  for (const viewport of viewports) {
    await setViewport(client, viewport.width, viewport.height, viewport.mobile);
    for (const pagePath of paths) {
      await navigate(client, `${baseUrl}${pagePath}`);
      const metrics = await readLayoutMetrics(client);
      pages.push({ viewport: viewport.name, path: pagePath, ...metrics });
    }
  }
  const pageIssues = pages.flatMap((page) => {
    const issues = [];
    if (page.horizontalOverflow) {
      issues.push({
        viewport: page.viewport,
        path: page.path,
        code: "horizontal_overflow",
        detail: `${page.scrollWidth}px scroll width in ${page.viewportWidth}px viewport`,
      });
    }
    if (page.productGridOverlaps.length) {
      issues.push({
        viewport: page.viewport,
        path: page.path,
        code: "product_grid_overlap",
        detail: `${page.productGridOverlaps.length} product-card overlaps`,
      });
    }
    if (page.mainHeight < 40 || page.bodyTextLength < 20) {
      issues.push({
        viewport: page.viewport,
        path: page.path,
        code: "empty_main_content",
        detail: `mainHeight=${page.mainHeight}, bodyTextLength=${page.bodyTextLength}`,
      });
    }
    return issues;
  });
  return result(
    "layout_smoke",
    "Layout smoke",
    pageIssues.length ? "fail" : "pass",
    {
      pagesChecked: pages.length,
      pages,
      issues: pageIssues,
    },
  );
}

async function capture(client, screenshotPath) {
  const screenshot = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await writeFile(screenshotPath, screenshot.data, "base64");
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const reportPath = path.resolve(args.report);
  const desktopScreenshotPath = path.resolve(args.desktopScreenshot);
  const mobileScreenshotPath = path.resolve(args.mobileScreenshot);
  const profilePath = path.resolve(`.tmp\\chrome-theme-flows-${process.pid}`);
  await mkdir(path.dirname(reportPath), { recursive: true });
  await mkdir(path.dirname(desktopScreenshotPath), { recursive: true });
  await mkdir(path.dirname(mobileScreenshotPath), { recursive: true });
  await mkdir(profilePath, { recursive: true });

  const chrome = spawn(
    args.chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--window-size=1440,900",
      `--remote-debugging-port=${args.port}`,
      `--user-data-dir=${profilePath}`,
      "about:blank",
    ],
    {
      stdio: "ignore",
      windowsHide: true,
    },
  );

  let client;
  try {
    const target = await waitForTarget(args.port, "about:blank");
    client = createCdpClient(target.webSocketDebuggerUrl);
    const consoleMessages = [];
    const documentResponses = [];
    const networkFailures = [];
    client.on("Runtime.consoleAPICalled", (event) => {
      consoleMessages.push({
        type: event.type,
        values: (event.args || []).map(
          (item) => item.value ?? item.description ?? item.type,
        ),
      });
    });
    client.on("Runtime.exceptionThrown", (event) => {
      consoleMessages.push({
        type: "exception",
        values: [
          event.exceptionDetails?.exception?.description ||
            event.exceptionDetails?.text ||
            "Unknown exception",
        ],
      });
    });
    client.on("Network.responseReceived", (event) => {
      if (event.type !== "Document") return;
      documentResponses.push({
        url: event.response.url,
        status: event.response.status,
        mimeType: event.response.mimeType,
      });
    });
    client.on("Network.loadingFailed", (event) => {
      networkFailures.push({
        requestId: event.requestId,
        errorText: event.errorText,
        blockedReason: event.blockedReason || "",
      });
    });
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Network.enable");

    await setViewport(client, 1440, 900, false);
    await navigate(client, `${baseUrl}/collections/drakes`);
    const desktopHeader = await readHeaderState(client);
    const flows = [
      result(
        "desktop_header",
        "Desktop header",
        desktopHeader.headerVisible &&
          desktopHeader.logoVisible &&
          (desktopHeader.navLinkCount > 0 || desktopHeader.menuVisible) &&
          desktopHeader.searchVisible &&
          desktopHeader.cartVisible
          ? "pass"
          : "fail",
        desktopHeader,
      ),
      await runSearchFlow(client, baseUrl),
      await runCartFlow(client, baseUrl),
    ];
    await capture(client, desktopScreenshotPath);
    flows.push(await runAccountFlow(client, baseUrl));
    flows.push(await runMobileMenuFlow(client, baseUrl));
    await capture(client, mobileScreenshotPath);
    flows.push(
      await runButtonSizingFlow(
        client,
        baseUrl,
        args.regularProduct,
        args.contactProduct,
      ),
    );
    flows.push(await runLayoutSmokeFlow(client, baseUrl));

    const issues = flows
      .filter((item) => item.status === "fail")
      .map((item) => ({
        code: `${item.key}_failed`,
        severity: "error",
        detail: `${item.label} did not meet the expected state.`,
      }));
    const summary = {
      flowsChecked: flows.length,
      passed: flows.filter((item) => item.status === "pass").length,
      notApplicable: flows.filter((item) => item.status === "not_applicable").length,
      errors: issues.length,
      warnings: 0,
    };
    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "read_only_browser_flow_audit",
      baseUrl,
      summary,
      flows,
      issues,
      consoleMessages,
      documentResponses,
      networkFailures,
      screenshots: {
        desktop: desktopScreenshotPath,
        mobile: mobileScreenshotPath,
      },
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    process.stdout.write(
      `${JSON.stringify(summary, null, 2)}\nReport: ${reportPath}\nDesktop screenshot: ${desktopScreenshotPath}\nMobile screenshot: ${mobileScreenshotPath}\n`,
    );
    process.exitCode = issues.length ? 1 : 0;
  } finally {
    client?.close();
    if (!chrome.killed) chrome.kill();
    await sleep(500);
    try {
      await rm(profilePath, {
        recursive: true,
        force: true,
        maxRetries: 5,
        retryDelay: 250,
      });
    } catch (error) {
      process.stderr.write(
        `Warning: could not remove Chrome profile ${profilePath}: ${error.message}\n`,
      );
    }
  }
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
