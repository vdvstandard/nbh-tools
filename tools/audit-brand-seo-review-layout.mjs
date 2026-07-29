#!/usr/bin/env node

import { spawn } from "node:child_process";
import { mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";


const DEFAULT_CHROME =
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";


function parseArgs(argv) {
  const args = {
    chrome: DEFAULT_CHROME,
    file: "brand-seo-review-20260725.html",
    width: 390,
    height: 844,
    port: 9342,
    report: ".tmp\\brand-seo-review-layout-mobile.json",
    screenshot: ".tmp\\brand-seo-review-layout-mobile.png",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--chrome") args.chrome = value;
    else if (key === "--file") args.file = value;
    else if (key === "--width") args.width = Number(value);
    else if (key === "--height") args.height = Number(value);
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
    await sleep(200);
  }
  throw new Error(
    `Document did not finish loading: ${JSON.stringify(lastState)}`,
  );
}


async function readLayout(client) {
  return evaluate(
    client,
    `(() => {
      const root = document.documentElement;
      const viewportWidth = root.clientWidth;
      const overflowElements = [...document.querySelectorAll('body *')]
        .filter((element) => {
          if (element.closest('pre')) return false;
          const style = getComputedStyle(element);
          if (style.display === 'none' || style.visibility === 'hidden') {
            return false;
          }
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && (
            rect.left < -0.5 ||
            rect.right > viewportWidth + 0.5
          );
        })
        .slice(0, 30)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            tag: element.tagName.toLowerCase(),
            className: element.className || '',
            id: element.id || '',
            left: Math.round(rect.left * 10) / 10,
            right: Math.round(rect.right * 10) / 10,
            width: Math.round(rect.width * 10) / 10
          };
        });
      return {
        innerWidth: window.innerWidth,
        clientWidth: viewportWidth,
        scrollWidth: root.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
        reviewCount: document.querySelectorAll('.brand-review').length,
        filterCount: document.querySelectorAll('.filter-button').length,
        approvalNoticePresent:
          document.body.textContent.includes('Pending approval. No Shopify write.'),
        overflowElements
      };
    })()`,
  );
}


async function run() {
  const args = parseArgs(process.argv.slice(2));
  const inputPath = path.resolve(args.file);
  const url = pathToFileURL(inputPath).href;
  const reportPath = path.resolve(args.report);
  const screenshotPath = path.resolve(args.screenshot);
  const profilePath = path.resolve(
    ".tmp",
    `chrome-brand-seo-review-${args.port}`,
  );
  await mkdir(path.dirname(reportPath), { recursive: true });
  await mkdir(path.dirname(screenshotPath), { recursive: true });
  await rm(profilePath, { recursive: true, force: true });
  await mkdir(profilePath, { recursive: true });

  const chrome = spawn(
    args.chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--allow-file-access-from-files",
      `--remote-debugging-port=${args.port}`,
      `--user-data-dir=${profilePath}`,
      "about:blank",
    ],
    { stdio: "ignore", windowsHide: true },
  );
  let client;
  try {
    const target = await waitForTarget(args.port);
    client = createCdpClient(target.webSocketDebuggerUrl);
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Emulation.setDeviceMetricsOverride", {
      width: args.width,
      height: args.height,
      deviceScaleFactor: 1,
      mobile: true,
      screenWidth: args.width,
      screenHeight: args.height,
    });
    await client.send("Page.navigate", { url });
    await waitForDocument(client, url);
    await sleep(300);

    const initial = await readLayout(client);
    const filterResult = await evaluate(
      client,
      `(() => {
        document.querySelector('[data-filter="rewrite"]').click();
        return {
          visibleReviews: [...document.querySelectorAll('.brand-review')]
            .filter((item) => !item.hidden).length,
          activeFilter: document.querySelector('.filter-button.is-active')
            ?.dataset.filter || ''
        };
      })()`,
    );
    await evaluate(
      client,
      "document.querySelector('[data-filter=\"all\"]').click()",
    );

    const screenshot = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    });
    await writeFile(screenshotPath, screenshot.data, "base64");

    const issues = [];
    if (initial.scrollWidth !== initial.clientWidth) {
      issues.push({
        code: "horizontal_document_overflow",
        clientWidth: initial.clientWidth,
        scrollWidth: initial.scrollWidth,
      });
    }
    if (initial.overflowElements.length) {
      issues.push({
        code: "elements_outside_viewport",
        elements: initial.overflowElements,
      });
    }
    if (initial.reviewCount !== 34) {
      issues.push({
        code: "review_count_mismatch",
        expected: 34,
        actual: initial.reviewCount,
      });
    }
    if (initial.filterCount !== 5) {
      issues.push({
        code: "filter_count_mismatch",
        expected: 5,
        actual: initial.filterCount,
      });
    }
    if (
      filterResult.activeFilter !== "rewrite" ||
      filterResult.visibleReviews !== 13
    ) {
      issues.push({
        code: "rewrite_filter_failed",
        actual: filterResult,
      });
    }
    if (!initial.approvalNoticePresent) {
      issues.push({ code: "approval_notice_missing" });
    }

    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "local_layout_audit",
      shopifyWritePerformed: false,
      file: inputPath,
      viewport: { width: args.width, height: args.height },
      initial,
      filterResult,
      issues,
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
          viewport: report.viewport,
          clientWidth: initial.clientWidth,
          scrollWidth: initial.scrollWidth,
          reviews: initial.reviewCount,
          rewriteFilterResults: filterResult.visibleReviews,
          issues: issues.length,
          shopifyWrites: 0,
        },
        null,
        2,
      )}\nReport: ${reportPath}\nScreenshot: ${screenshotPath}\n`,
    );
    process.exitCode = issues.length ? 1 : 0;
  } finally {
    client?.close();
    if (!chrome.killed) chrome.kill();
    await sleep(400);
    await rm(profilePath, { recursive: true, force: true });
  }
}


run().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
