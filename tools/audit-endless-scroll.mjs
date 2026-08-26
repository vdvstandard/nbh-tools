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
    url: "http://127.0.0.1:9292/collections/drakes",
    expected: 53,
    port: 9333,
    report: ".tmp\\phase3-drakes-endless-scroll.json",
    screenshot: ".tmp\\phase3-drakes-endless-scroll.png",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--chrome") args.chrome = value;
    else if (key === "--url") args.url = value;
    else if (key === "--expected") args.expected = Number(value);
    else if (key === "--port") args.port = Number(value);
    else if (key === "--report") args.report = value;
    else if (key === "--screenshot") args.screenshot = value;
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

async function waitForDocument(client, expectedUrl) {
  let lastState = {};
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const state = await evaluate(
      client,
      `({ readyState: document.readyState, url: window.location.href })`,
    );
    lastState = state;
    if (
      state.readyState === "complete" &&
      state.url.startsWith(expectedUrl)
    ) {
      return;
    }
    await sleep(250);
  }
  throw new Error(
    `Document did not finish loading: ${JSON.stringify(lastState)}`,
  );
}

async function readMetrics(client) {
  return evaluate(
    client,
    `(() => {
      const grid = document.querySelector('[data-endless-scroll-grid]');
      const control = document.querySelector('[data-endless-scroll]');
      const area = document.querySelector('[data-endless-scroll-area]');
      const productCountText =
        document.getElementById('ProductCount')?.textContent
          .replace(/\\s+/g, ' ')
          .trim() || '';
      const productCountMatch = productCountText.match(/\\d+/);
      return {
        cards: grid ? grid.querySelectorAll(':scope > .grid__item').length : 0,
        productCountText,
        productCountValue: productCountMatch ? Number(productCountMatch[0]) : null,
        nextUrl: control?.dataset.nextUrl || '',
        controlPresent: Boolean(control),
        controlHidden: control ? control.hidden : null,
        areaClasses: area?.className || '',
        visibleLoadMore: [...document.querySelectorAll('body *')].some((node) =>
          node.children.length === 0 &&
          node.textContent.trim().toLowerCase() === 'load more' &&
          !node.hidden &&
          getComputedStyle(node).display !== 'none' &&
          getComputedStyle(node).visibility !== 'hidden'
        ),
        scrollHeight: document.documentElement.scrollHeight,
        scrollY: window.scrollY
      };
    })()`,
  );
}

async function runReturnPositionFlow(client, collectionUrl) {
  const selected = await evaluate(
    client,
    `(() => {
      const items = [...document.querySelectorAll('[data-endless-scroll-grid] > .grid__item')];
      const item = items.at(-1);
      const link = item
        ? [...item.querySelectorAll('a[href]')].find((candidate) =>
            new URL(candidate.href, location.origin).pathname.includes('/products/')
          )
        : null;
      if (!item || !link) return null;
      item.scrollIntoView({ block: 'center' });
      return {
        cardsBeforeNavigation: items.length,
        productPath: new URL(link.href, location.origin).pathname,
        productUrl: link.href
      };
    })()`,
  );

  if (!selected) {
    return { passed: false, error: "No product card was available for the return-position flow." };
  }

  await sleep(250);
  const viewportOffset = await evaluate(
    client,
    `(() => {
      const productPath = ${JSON.stringify(selected.productPath)};
      const item = [...document.querySelectorAll('[data-endless-scroll-grid] > .grid__item')].find(
        (candidate) => [...candidate.querySelectorAll('a[href]')].some(
          (link) => new URL(link.href, location.origin).pathname === productPath
        )
      );
      return item?.getBoundingClientRect().top ?? null;
    })()`,
  );

  await evaluate(
    client,
    `(() => {
      const productPath = ${JSON.stringify(selected.productPath)};
      const link = [...document.querySelectorAll('[data-endless-scroll-grid] > .grid__item a[href]')].find(
        (candidate) => new URL(candidate.href, location.origin).pathname === productPath
      );
      link?.click();
    })()`,
  );
  await waitForDocument(client, selected.productUrl);
  await evaluate(client, "history.back()");
  await waitForDocument(client, collectionUrl);

  let restored = null;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    restored = await evaluate(
      client,
      `(() => {
        const productPath = ${JSON.stringify(selected.productPath)};
        const grid = document.querySelector('[data-endless-scroll-grid]');
        const item = grid
          ? [...grid.querySelectorAll(':scope > .grid__item')].find(
              (candidate) => [...candidate.querySelectorAll('a[href]')].some(
                (link) => new URL(link.href, location.origin).pathname === productPath
              )
            )
          : null;
        return {
          cardsAfterReturn: grid?.querySelectorAll(':scope > .grid__item').length || 0,
          productFound: Boolean(item),
          viewportOffset: item?.getBoundingClientRect().top ?? null,
          navigationType: performance.getEntriesByType('navigation')[0]?.type || ''
        };
      })()`,
    );
    if (
      restored.productFound &&
      Math.abs(restored.viewportOffset - viewportOffset) <= 2
    ) {
      break;
    }
    await sleep(250);
  }

  const offsetDifference =
    restored?.viewportOffset === null || viewportOffset === null
      ? null
      : Math.abs(restored.viewportOffset - viewportOffset);

  return {
    passed:
      Boolean(restored?.productFound) &&
      offsetDifference !== null &&
      offsetDifference <= 2,
    ...selected,
    expectedViewportOffset: viewportOffset,
    offsetDifference,
    ...restored,
  };
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const reportPath = path.resolve(args.report);
  const screenshotPath = path.resolve(args.screenshot);
  const profilePath = path.resolve(
    `.tmp\\chrome-endless-scroll-${process.pid}`,
  );
  await mkdir(path.dirname(reportPath), { recursive: true });
  await mkdir(path.dirname(screenshotPath), { recursive: true });
  await mkdir(profilePath, { recursive: true });

  const chrome = spawn(
    args.chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
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
    const networkResponses = [];
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
      if (!event.response?.url.includes("/collections/")) return;
      networkResponses.push({
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
    await client.send("Page.navigate", { url: args.url });
    await waitForDocument(client, args.url);

    const rounds = [];
    let stableRounds = 0;
    let previousCards = -1;
    for (let round = 0; round < 12; round += 1) {
      const before = await readMetrics(client);
      await evaluate(
        client,
        "window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' })",
      );
      await sleep(1500);
      const after = await readMetrics(client);
      rounds.push({ round: round + 1, before, after });

      stableRounds =
        after.cards === previousCards ? stableRounds + 1 : 0;
      previousCards = after.cards;
      if (!after.nextUrl && after.cards >= args.expected) break;
      if (stableRounds >= 3) break;
    }

    await evaluate(
      client,
      "window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' })",
    );
    await sleep(500);
    const final = await readMetrics(client);
    const returnPosition = await runReturnPositionFlow(client, args.url);
    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    });
    await writeFile(screenshotPath, screenshot.data, "base64");

    const issues = [];
    if (final.cards !== args.expected) {
      issues.push({
        code: "final_product_count_mismatch",
        expected: args.expected,
        actual: final.cards,
      });
    }
    if (
      final.productCountValue !== null &&
      final.productCountValue !== args.expected
    ) {
      issues.push({
        code: "product_count_label_mismatch",
        expected: args.expected,
        actual: final.productCountValue,
        text: final.productCountText,
      });
    }
    if (final.nextUrl) {
      issues.push({
        code: "next_url_remains_after_last_page",
        nextUrl: final.nextUrl,
      });
    }
    if (final.controlPresent && !final.controlHidden) {
      issues.push({ code: "endless_control_not_hidden" });
    }
    if (final.visibleLoadMore) {
      issues.push({ code: "visible_load_more" });
    }
    if (!returnPosition.passed) {
      issues.push({
        code: "product_return_position_mismatch",
        expectedViewportOffset: returnPosition.expectedViewportOffset ?? null,
        actualViewportOffset: returnPosition.viewportOffset ?? null,
        offsetDifference: returnPosition.offsetDifference ?? null,
        error: returnPosition.error || "",
      });
    }

    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "read_only_browser_audit",
      url: args.url,
      expectedProducts: args.expected,
      rounds,
      final,
      returnPosition,
      issues,
      consoleMessages,
      networkResponses,
      networkFailures,
      screenshot: screenshotPath,
    };
    await writeFile(
      reportPath,
      `${JSON.stringify(report, null, 2)}\n`,
      "utf8",
    );
    process.stdout.write(
      `${JSON.stringify(
        {
          expectedProducts: args.expected,
          finalProducts: final.cards,
          finalProductCountText: final.productCountText,
          finalProductCountValue: final.productCountValue,
          nextUrl: final.nextUrl,
          controlHidden: final.controlHidden,
          visibleLoadMore: final.visibleLoadMore,
          returnPositionPassed: returnPosition.passed,
          returnPositionOffsetDifference: returnPosition.offsetDifference ?? null,
          issues: issues.length,
        },
        null,
        2,
      )}\nReport: ${reportPath}\nScreenshot: ${screenshotPath}\n`,
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
