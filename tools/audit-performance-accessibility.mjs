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
    port: 9370,
    connectToExistingChrome: false,
    report: ".tmp\\phase6-performance-accessibility-20260729.json",
    pages: [
      ["home", "/"],
      ["collection-drakes", "/collections/drakes"],
      ["collection-filter-drakes", "/collections/all?filter.p.m.custom.brand=Drake%27s"],
      ["product-regular", "/products/thuy-t-shirt-aran-creme"],
      ["product-in-store-exclusive", "/products/beira-solid-black-dark-grey"],
      ["lookbook", "/pages/lookbook"],
      ["cart", "/cart"],
    ],
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
    }
    else if (key === "--report") args.report = value;
    else if (key === "--pages") {
      args.pages = value.split(",").map((entry) => {
        const [name, pagePath] = entry.split("=");
        return [name, pagePath || name];
      });
    } else continue;
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

async function setViewport(client, viewport) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: viewport.mobile,
  });
}

async function waitForDocument(client, expectedPrefix) {
  let lastState = {};
  for (let attempt = 0; attempt < 100; attempt += 1) {
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

function summarizeResources(resources) {
  const empty = () => ({ count: 0, transferSize: 0, encodedBodySize: 0 });
  const groups = {
    script: empty(),
    css: empty(),
    image: empty(),
    font: empty(),
    fetch: empty(),
    other: empty(),
  };

  for (const resource of resources) {
    const key = groups[resource.initiatorType] ? resource.initiatorType : "other";
    groups[key].count += 1;
    groups[key].transferSize += resource.transferSize || 0;
    groups[key].encodedBodySize += resource.encodedBodySize || 0;
  }

  return groups;
}

function createIssue(code, severity, detail, data = {}) {
  return { code, severity, detail, ...data };
}

async function collectDomAudit(client) {
  return evaluate(
    client,
    `(() => {
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight;
      const isVisible = (element) => {
        if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return rect.width > 0 &&
          rect.height > 0 &&
          style.display !== 'none' &&
          style.visibility !== 'hidden' &&
          style.opacity !== '0';
      };
      const isInInitialViewport = (element) => {
        const rect = element.getBoundingClientRect();
        return rect.bottom > 0 && rect.top < viewportHeight && rect.right > 0 && rect.left < viewportWidth;
      };
      const accessibleName = (element) => {
        const childImageAlt = element.querySelector('img[alt]')?.getAttribute('alt') || '';
        return (element.getAttribute('aria-label') ||
          element.getAttribute('title') ||
          element.getAttribute('alt') ||
          childImageAlt ||
          element.textContent ||
          '').replace(/\\s+/g, ' ').trim();
      };

      const images = [...document.images].map((image) => {
        const rect = image.getBoundingClientRect();
        const renderedWidth = Math.round(rect.width);
        const renderedHeight = Math.round(rect.height);
        const oversizeRatio = renderedWidth > 0
          ? Math.round((image.naturalWidth / renderedWidth) * 100) / 100
          : null;
        return {
          src: image.currentSrc || image.src,
          alt: image.getAttribute('alt'),
          loading: image.getAttribute('loading') || '',
          fetchpriority: image.getAttribute('fetchpriority') || '',
          widthAttr: image.getAttribute('width') || '',
          heightAttr: image.getAttribute('height') || '',
          naturalWidth: image.naturalWidth,
          naturalHeight: image.naturalHeight,
          renderedWidth,
          renderedHeight,
          oversizeRatio,
          visible: isVisible(image),
          initialViewport: isVisible(image) && isInInitialViewport(image),
        };
      });

      const focusableSelector = [
        'a[href]',
        'button:not([disabled])',
        'summary',
        'input:not([disabled]):not([type="hidden"])',
        'select:not([disabled])',
        'textarea:not([disabled])',
        '[tabindex]:not([tabindex^="-"])'
      ].join(',');

      const focusables = [...document.querySelectorAll(focusableSelector)].map((element) => {
        const rect = element.getBoundingClientRect();
        const hiddenAncestor = element.closest('[aria-hidden="true"]');
        const inertAncestor = element.closest('[inert]');
        const visible = isVisible(element) && !inertAncestor;
        return {
          tag: element.tagName.toLowerCase(),
          id: element.getAttribute('id') || '',
          className: typeof element.className === 'string' ? element.className : '',
          href: element.href || '',
          name: accessibleName(element),
          visible,
          initialViewport: visible && isInInitialViewport(element),
          ariaHidden: Boolean(hiddenAncestor),
          inert: Boolean(inertAncestor),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      });

      const duplicateIds = Object.entries(
        [...document.querySelectorAll('[id]')].reduce((groups, element) => {
          const id = element.getAttribute('id') || '';
          if (!id) return groups;
          groups[id] = groups[id] || [];
          groups[id].push(element);
          return groups;
        }, {})
      ).filter(([, elements]) => elements.length > 1).map(([id, elements]) => ({
        id,
        count: elements.length,
        examples: elements.slice(0, 3).map((element) => element.outerHTML.replace(/\\s+/g, ' ').slice(0, 240)),
      }));

      const activeAnimations = [...document.querySelectorAll('*')]
        .filter(isVisible)
        .slice(0, 800)
        .map((element) => {
          const style = getComputedStyle(element);
          return {
            tag: element.tagName.toLowerCase(),
            className: typeof element.className === 'string' ? element.className : '',
            animationName: style.animationName,
            animationDuration: style.animationDuration,
          };
        })
        .filter((item) => item.animationName !== 'none' && !item.animationDuration.split(',').every((duration) => duration.trim() === '0s'));

      return {
        title: document.title,
        bodyTextLength: document.body.innerText.replace(/\\s+/g, ' ').trim().length,
        viewportWidth,
        viewportHeight,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
        images,
        focusables,
        duplicateIds,
        activeAnimations,
      };
    })()`,
  );
}

async function collectPageAudit(client, url, viewport) {
  await setViewport(client, viewport);
  await client.send("Network.enable");
  await client.send("Performance.enable");
  await client.send("Page.navigate", { url });
  await waitForDocument(client, url);
  await dismissCookieBanner(client);
  await sleep(1000);

  const [navigation, resources, performanceMetrics, dom] = await Promise.all([
    evaluate(client, `performance.getEntriesByType('navigation')[0]?.toJSON() || {}`),
    evaluate(client, `performance.getEntriesByType('resource').map((entry) => entry.toJSON())`),
    client.send("Performance.getMetrics"),
    collectDomAudit(client),
  ]);

  await client.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  await sleep(250);
  const reducedMotion = await collectDomAudit(client);
  await client.send("Emulation.setEmulatedMedia", { features: [] });

  const resourceSummary = summarizeResources(resources);
  const metrics = Object.fromEntries(
    performanceMetrics.metrics.map((metric) => [metric.name, metric.value]),
  );

  const issues = [];
  if (dom.scrollWidth > dom.viewportWidth + 1) {
    issues.push(
      createIssue(
        "horizontal_overflow",
        "error",
        `Document scroll width ${dom.scrollWidth}px exceeds viewport ${dom.viewportWidth}px.`,
      ),
    );
  }

  const visibleImagesMissingAlt = dom.images.filter(
    (image) => image.visible && image.alt === null,
  );
  if (visibleImagesMissingAlt.length) {
    issues.push(
      createIssue(
        "image_missing_alt_attribute",
        "error",
        `${visibleImagesMissingAlt.length} visible image(s) are missing an alt attribute.`,
        { examples: visibleImagesMissingAlt.slice(0, 5).map((image) => image.src) },
      ),
    );
  }

  const visibleImagesMissingDimensions = dom.images.filter(
    (image) => image.visible && (!image.widthAttr || !image.heightAttr),
  );
  if (visibleImagesMissingDimensions.length) {
    issues.push(
      createIssue(
        "image_missing_dimensions",
        "warning",
        `${visibleImagesMissingDimensions.length} visible image(s) are missing width or height attributes.`,
        { examples: visibleImagesMissingDimensions.slice(0, 5).map((image) => image.src) },
      ),
    );
  }

  const oversizedImages = dom.images.filter(
    (image) => image.visible && image.renderedWidth >= 80 && image.oversizeRatio && image.oversizeRatio > 2.8,
  );
  if (oversizedImages.length) {
    issues.push(
      createIssue(
        "oversized_images",
        "warning",
        `${oversizedImages.length} visible image(s) are more than 2.8x wider than rendered size.`,
        {
          examples: oversizedImages.slice(0, 5).map((image) => ({
            src: image.src,
            renderedWidth: image.renderedWidth,
            naturalWidth: image.naturalWidth,
            oversizeRatio: image.oversizeRatio,
          })),
        },
      ),
    );
  }

  const initialLazyImages = dom.images.filter(
    (image) => image.initialViewport && image.renderedWidth >= 240 && image.loading === "lazy",
  );
  if (initialLazyImages.length) {
    issues.push(
      createIssue(
        "initial_viewport_image_lazy_loaded",
        "warning",
        `${initialLazyImages.length} large initial-viewport image(s) are lazy loaded.`,
        { examples: initialLazyImages.slice(0, 5).map((image) => image.src) },
      ),
    );
  }

  const unnamedControls = dom.focusables.filter(
    (item) => item.visible && !item.name && ["a", "button", "summary"].includes(item.tag),
  );
  if (unnamedControls.length) {
    issues.push(
      createIssue(
        "interactive_missing_name",
        "error",
        `${unnamedControls.length} visible interactive element(s) are missing accessible names.`,
        { examples: unnamedControls.slice(0, 8) },
      ),
    );
  }

  const hiddenFocusable = dom.focusables.filter((item) => item.ariaHidden && !item.inert);
  if (hiddenFocusable.length) {
    issues.push(
      createIssue(
        "focusable_inside_aria_hidden",
        "error",
        `${hiddenFocusable.length} focusable element(s) are inside aria-hidden content.`,
        { examples: hiddenFocusable.slice(0, 8) },
      ),
    );
  }

  if (dom.duplicateIds.length) {
    issues.push(
      createIssue(
        "duplicate_ids",
        "error",
        `${dom.duplicateIds.length} duplicate id value(s) found.`,
        { examples: dom.duplicateIds.slice(0, 8) },
      ),
    );
  }

  if (reducedMotion.activeAnimations.length) {
    issues.push(
      createIssue(
        "reduced_motion_active_animations",
        "warning",
        `${reducedMotion.activeAnimations.length} visible element(s) still report active CSS animations with reduced motion emulated.`,
        { examples: reducedMotion.activeAnimations.slice(0, 8) },
      ),
    );
  }

  if (resourceSummary.script.count > 28) {
    issues.push(
      createIssue(
        "many_script_resources",
        "warning",
        `${resourceSummary.script.count} script resources were loaded.`,
      ),
    );
  }

  return {
    viewport: viewport.name,
    url,
    status: issues.some((issue) => issue.severity === "error") ? "fail" : "pass",
    navigation,
    metrics,
    resources: resourceSummary,
    dom: {
      title: dom.title,
      bodyTextLength: dom.bodyTextLength,
      viewportWidth: dom.viewportWidth,
      viewportHeight: dom.viewportHeight,
      scrollWidth: dom.scrollWidth,
      scrollHeight: dom.scrollHeight,
      imageCount: dom.images.length,
      visibleImageCount: dom.images.filter((image) => image.visible).length,
      focusableCount: dom.focusables.length,
      initialViewportFocusableCount: dom.focusables.filter((item) => item.initialViewport).length,
      duplicateIds: dom.duplicateIds,
    },
    issues,
  };
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const reportPath = path.resolve(args.report);
  const profilePath = path.resolve(`.tmp\\chrome-phase6-${process.pid}`);
  await mkdir(path.dirname(reportPath), { recursive: true });
  if (!args.connectToExistingChrome) {
    await mkdir(profilePath, { recursive: true });
  }

  const viewports = [
    { name: "desktop", width: 1440, height: 900, mobile: false },
    { name: "mobile", width: 390, height: 844, mobile: true },
  ];

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
      {
        stdio: "ignore",
        windowsHide: true,
      },
    );
  }

  let client;
  try {
    const target = await waitForTarget(args.port, "about:blank");
    client = createCdpClient(target.webSocketDebuggerUrl);
    await client.send("Page.enable");
    await client.send("Runtime.enable");

    const results = [];
    for (const viewport of viewports) {
      for (const [name, pagePath] of args.pages) {
        const url = `${baseUrl}${pagePath}`;
        try {
          const audit = await collectPageAudit(client, url, viewport);
          results.push({ name, path: pagePath, ...audit });
        } catch (error) {
          results.push({
            name,
            path: pagePath,
            viewport: viewport.name,
            url,
            status: "error",
            error: String(error?.message || error),
            issues: [
              createIssue(
                "audit_failed",
                "error",
                `Audit failed for ${viewport.name} ${pagePath}.`,
              ),
            ],
          });
        }
      }
    }

    const issueCounts = results.reduce(
      (counts, result) => {
        for (const issue of result.issues || []) {
          counts.total += 1;
          counts[issue.severity] = (counts[issue.severity] || 0) + 1;
        }
        return counts;
      },
      { total: 0 },
    );

    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "phase6_performance_accessibility_cdp_audit",
      baseUrl,
      viewports,
      summary: {
        pages: args.pages.length,
        runs: results.length,
        issueCounts,
        failedRuns: results.filter((result) => result.status !== "pass").length,
      },
      results,
    };

    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report.summary, null, 2));
    console.log(`Report: ${reportPath}`);
  } finally {
    client?.close();
    if (chrome && !chrome.killed) chrome.kill();
    if (!args.connectToExistingChrome) {
      await rm(profilePath, { recursive: true, force: true });
    }
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
