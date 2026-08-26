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
    port: 9360,
    outputDir: ".tmp\\phase4-visual-screenshots-20260729",
    report: ".tmp\\phase4-visual-screenshots-20260729.json",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--chrome") args.chrome = value;
    else if (key === "--base-url") args.baseUrl = value;
    else if (key === "--port") args.port = Number(value);
    else if (key === "--output-dir") args.outputDir = value;
    else if (key === "--report") args.report = value;
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
  let commandId = 0;

  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
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

async function waitForDocument(client, expectedPrefix) {
  let lastState = {};
  for (let attempt = 0; attempt < 80; attempt += 1) {
    lastState = await evaluate(
      client,
      `({
        ready: document.readyState,
        url: window.location.href
      })`,
    );
    if (
      lastState.ready === "complete" &&
      lastState.url.startsWith(expectedPrefix)
    ) {
      return;
    }
    await sleep(250);
  }
  throw new Error(
    `Document did not finish loading: ${JSON.stringify(lastState)}`,
  );
}

async function dismissCookieBanner(client) {
  const clicked = await evaluate(
    client,
    `(() => {
      const button = [...document.querySelectorAll('button')].find((item) =>
        item.textContent.replace(/\\s+/g, ' ').trim().toLowerCase() === 'decline'
      );
      if (!button) return false;
      button.click();
      return true;
    })()`,
  );
  if (clicked) await sleep(250);
}

async function setViewport(client, viewport) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: viewport.mobile,
  });
}

async function capturePage(client, url, outputPath) {
  await client.send("Page.navigate", { url });
  await waitForDocument(client, url);
  await dismissCookieBanner(client);
  await evaluate(client, "window.scrollTo({ top: 0, behavior: 'instant' })");
  await sleep(1000);
  const metrics = await evaluate(
    client,
    `(() => ({
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      viewportWidth: document.documentElement.clientWidth,
      viewportHeight: window.innerHeight,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
      bodyTextLength: document.body.innerText.replace(/\\s+/g, ' ').trim().length
    }))()`,
  );
  const screenshot = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await writeFile(outputPath, screenshot.data, "base64");
  return metrics;
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const reportPath = path.resolve(args.report);
  const outputDir = path.resolve(args.outputDir);
  const profilePath = path.resolve(
    `.tmp\\chrome-theme-screenshots-${process.pid}`,
  );
  await mkdir(path.dirname(reportPath), { recursive: true });
  await mkdir(outputDir, { recursive: true });
  await mkdir(profilePath, { recursive: true });

  const pages = [
    ["home", "/"],
    ["collection-drakes", "/collections/drakes"],
    ["collection-filter-drakes", "/collections/all?filter.p.m.custom.brand=Drake%27s"],
    ["collection-akog", "/collections/a-kind-of-guise"],
    ["collection-in-store-exclusive", "/collections/in-store-exclusive"],
    ["product-regular", "/products/beanie-onyx"],
    ["product-in-store-exclusive", "/products/thuy-t-shirt-aran-creme"],
    ["search-drake", "/search?q=drake&options%5Bprefix%5D=last"],
    ["contact-policy", "/policies/contact-information"],
    ["journal", "/blogs/journal"],
    ["journal-article", "/blogs/journal/a-kind-of-guise"],
    ["lookbook", "/pages/lookbook"],
    ["cart", "/cart"],
  ];
  const viewports = [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    { name: "mobile", width: 390, height: 844, mobile: true },
  ];

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
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    const screenshots = [];
    for (const viewport of viewports) {
      await setViewport(client, viewport);
      for (const [name, pagePath] of pages) {
        const fileName = `${viewport.name}-${name}.png`;
        const screenshotPath = path.join(outputDir, fileName);
        const metrics = await capturePage(
          client,
          `${baseUrl}${pagePath}`,
          screenshotPath,
        );
        screenshots.push({
          viewport: viewport.name,
          name,
          path: pagePath,
          screenshot: screenshotPath,
          metrics,
        });
      }
    }
    const issues = screenshots
      .filter((item) => item.metrics.bodyTextLength < 20)
      .map((item) => ({
        code: "blank_screenshot_candidate",
        severity: "error",
        detail: `${item.viewport} ${item.name} has very little body text.`,
      }));
    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "read_only_visual_screenshot_capture",
      baseUrl,
      outputDir,
      summary: {
        screenshots: screenshots.length,
        errors: issues.length,
        warnings: 0,
      },
      screenshots,
      issues,
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    process.stdout.write(
      `${JSON.stringify(report.summary, null, 2)}\nReport: ${reportPath}\nOutput directory: ${outputDir}\n`,
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
