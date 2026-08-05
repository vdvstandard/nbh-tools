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
    connectToExistingChrome: false,
    report: ".tmp\\phase5-cart-checkout-20260729.json",
    screenshotDir: ".tmp\\phase5-cart-checkout-20260729",
    product: "thuy-t-shirt-aran-creme",
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
    else if (key === "--screenshot-dir") args.screenshotDir = value;
    else if (key === "--product") args.product = value;
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
  await waitForCondition(
    client,
    `(() => ({
      ready: document.readyState === 'complete',
      url: window.location.href
    }))()`,
    "Document did not finish loading",
  );
  await waitForCondition(
    client,
    `document.readyState === 'complete' && window.location.href.startsWith(${JSON.stringify(expectedPrefix)})`,
    `Document did not reach ${expectedPrefix}`,
  );
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
    const textOf = (selectorOrElement) => {
      const element = typeof selectorOrElement === 'string'
        ? document.querySelector(selectorOrElement)
        : selectorOrElement;
      return element?.textContent.replace(/\\s+/g, ' ').trim() || '';
    };
  `;
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

async function clearCart(client, baseUrl) {
  await navigate(client, `${baseUrl}/cart`);
  await evaluate(
    client,
    `fetch('/cart/clear.js', { method: 'POST', headers: { Accept: 'application/json' } })
      .then((response) => response.json())`,
  );
  await sleep(500);
}

async function readCartJson(client) {
  return evaluate(
    client,
    `fetch('/cart.js', { headers: { Accept: 'application/json' } })
      .then(async (response) => {
        const text = await response.text();
        try {
          return JSON.parse(text);
        } catch {
          return {
            item_count: null,
            total_price: null,
            currency: '',
            items: [],
            parseError: true,
            status: response.status,
            bodyStart: text.slice(0, 300)
          };
        }
      })
      .then((cart) => ({
        itemCount: cart.item_count,
        totalPrice: cart.total_price,
        currency: cart.currency,
        parseError: cart.parseError || false,
        status: cart.status || null,
        bodyStart: cart.bodyStart || '',
        items: cart.items.map((item) => ({
          title: item.product_title,
          variantTitle: item.variant_title,
          quantity: item.quantity,
          price: item.price,
          finalLinePrice: item.final_line_price
        }))
      }))`,
  );
}

async function readShippingRates(client, address) {
  return evaluate(
    client,
    `(async () => {
      const address = ${JSON.stringify(address)};
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(address)) {
        params.set('shipping_address[' + key + ']', value);
      }
      const suffix = params.toString();
      const prepareResponse = await fetch('/cart/prepare_shipping_rates.json?' + suffix, {
        method: 'POST',
        headers: { Accept: 'application/json' }
      });
      const prepareText = await prepareResponse.text();
      let lastStatus = null;
      let lastBody = '';
      for (let attempt = 0; attempt < 16; attempt += 1) {
        const response = await fetch('/cart/async_shipping_rates.json?' + suffix, {
          headers: { Accept: 'application/json' }
        });
        const text = await response.text();
        lastStatus = response.status;
        lastBody = text.slice(0, 500);
        if (!response.ok) {
          return {
            address,
            prepareStatus: prepareResponse.status,
            prepareBody: prepareText.slice(0, 500),
            status: response.status,
            body: lastBody,
            rates: [],
            passed: false
          };
        }
        const parsed = text ? JSON.parse(text) : null;
        if (parsed?.shipping_rates) {
          return {
            address,
            prepareStatus: prepareResponse.status,
            prepareBody: prepareText.slice(0, 500),
            status: response.status,
            body: lastBody,
            rates: parsed.shipping_rates.map((rate) => ({
              name: rate.name,
              price: rate.price,
              source: rate.source,
              deliveryDate: rate.delivery_date,
              deliveryRange: rate.delivery_range
            })),
            passed: parsed.shipping_rates.length > 0
          };
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      return {
        address,
        prepareStatus: prepareResponse.status,
        prepareBody: prepareText.slice(0, 500),
        status: lastStatus,
        body: lastBody,
        rates: [],
        passed: false,
        timedOut: true
      };
    })()`,
  );
}

async function testShippingRates(client) {
  const destinations = [
    { label: "Netherlands", country: "NL", zip: "6811EV" },
    { label: "Germany", country: "DE", zip: "10115" },
    { label: "United States", country: "US", province: "NY", zip: "10001" },
  ];
  const results = [];
  for (const destination of destinations) {
    const { label, ...address } = destination;
    results.push({ label, ...(await readShippingRates(client, address)) });
  }
  return results;
}

async function addProduct(client, baseUrl, productHandle) {
  await navigate(client, `${baseUrl}/products/${productHandle}`);
  const productState = await evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const submit = document.querySelector('.product-form__submit');
      const pickup = document.querySelector('pickup-availability');
      return {
        url: location.href,
        title: document.title,
        submitVisible: isVisible(submit),
        submitDisabled: Boolean(submit?.disabled || submit?.getAttribute('aria-disabled') === 'true'),
        submitText: textOf(submit),
        hasPickupAvailability: Boolean(pickup),
        pickupAvailableAttribute: pickup?.hasAttribute('available') || false,
        pickupText: textOf(pickup),
        productTaxNote: textOf('.product__tax'),
        dynamicCheckoutButtonCount: document.querySelectorAll('.shopify-payment-button__button').length,
      };
    })()`,
  );
  const clicked = await evaluate(
    client,
    `(() => {
      const submit = document.querySelector('.product-form__submit:not([disabled])');
      if (!submit || submit.getAttribute('aria-disabled') === 'true') return false;
      submit.click();
      return true;
    })()`,
  );
  if (!clicked) {
    return { productState, added: false };
  }
  await waitForCondition(
    client,
    `fetch('/cart.js').then((cart) => cart.json()).then((cart) => cart.item_count > 0)`,
    "Product was not added to the cart",
  );
  await waitForCondition(
    client,
    `Boolean(document.querySelector('cart-drawer.active .cart-item'))`,
    "Cart drawer did not open with a line item",
  );
  await sleep(600);
  return { productState, added: true };
}

async function readDrawerState(client) {
  return evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const drawer = document.querySelector('cart-drawer');
      const lineItems = [...document.querySelectorAll('cart-drawer .cart-item')].map((item) => ({
        name: textOf(item.querySelector('.cart-item__name')),
        price: textOf(item.querySelector('.product-option')),
        total: textOf(item.querySelector('.cart-item__totals')),
        quantity: item.querySelector('.quantity__input')?.value || '',
        hasImage: Boolean(item.querySelector('.cart-item__image')),
        hasQuantityInput: isVisible(item.querySelector('.quantity__input')),
        hasPlus: Boolean(item.querySelector('.quantity__button[name="plus"]')),
        hasMinus: Boolean(item.querySelector('.quantity__button[name="minus"]')),
        hasRemove: Boolean(item.querySelector('cart-remove-button')),
        removeLabel: item.querySelector('cart-remove-button button, cart-remove-button a')?.getAttribute('aria-label') || '',
        discountText: textOf(item.querySelector('.discounts')),
        errorText: textOf(item.querySelector('.cart-item__error-text')),
      }));
      const surfaceText = textOf('cart-drawer');
      return {
        url: location.href,
        drawerPresent: Boolean(drawer),
        drawerActive: drawer?.classList.contains('active') || false,
        dialogVisible: isVisible('#CartDrawer .drawer__inner'),
        quantityInputDefined: Boolean(customElements.get('quantity-input')),
        cartDrawerItemsDefined: Boolean(customElements.get('cart-drawer-items')),
        lineItems,
        checkoutVisible: isVisible('#CartDrawer-Checkout'),
        checkoutDisabled: Boolean(document.querySelector('#CartDrawer-Checkout')?.disabled),
        checkoutText: textOf('#CartDrawer-Checkout'),
        estimatedTotal: textOf('cart-drawer .totals'),
        taxNote: textOf('cart-drawer .tax-note'),
        cartLevelDiscounts: textOf('cart-drawer .cart-drawer__footer .discounts'),
        cartErrors: textOf('#CartDrawer-CartErrors'),
        noteVisible: isVisible('#Details-CartDrawer'),
        dynamicCheckoutButtonCount: document.querySelectorAll('cart-drawer .additional-checkout-buttons, cart-drawer .cart__dynamic-checkout-buttons').length,
        hasDeliveryCopy: /delivery/i.test(surfaceText),
        hasShippingCopy: /shipping/i.test(surfaceText),
        hasPickupCopy: /pickup|pick up|store pickup/i.test(surfaceText),
        hasArnhemCopy: /arnhem/i.test(surfaceText),
      };
    })()`,
  );
}

async function clickDrawerQuantityPlus(client) {
  const before = await readCartJson(client);
  const clickState = await evaluate(
    client,
    `(() => {
      const button = document.querySelector('cart-drawer .cart-item .quantity__button[name="plus"]');
      const input = document.querySelector('cart-drawer .cart-item .quantity__input');
      if (!button || button.classList.contains('disabled')) {
        return {
          clicked: false,
          buttonPresent: Boolean(button),
          buttonClasses: button?.className || '',
          buttonName: button?.name || '',
          inputValueBefore: input?.value || '',
          inputValueAfter: input?.value || '',
          quantityInputDefined: Boolean(customElements.get('quantity-input'))
        };
      }
      const inputValueBefore = input?.value || '';
      button.click();
      return {
        clicked: true,
        buttonPresent: true,
        buttonClasses: button.className || '',
        buttonName: button.name || '',
        inputValueBefore,
        inputValueAfter: input?.value || '',
        quantityInputDefined: Boolean(customElements.get('quantity-input'))
      };
    })()`,
  );
  if (!clickState.clicked) return { ...clickState, passed: false, before, after: before };
  let after = before;
  const expected = before.itemCount + 1;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    after = await readCartJson(client);
    if (after.itemCount === expected) {
      await sleep(600);
      return { ...clickState, passed: true, before, after };
    }
    await sleep(250);
  }
  return { ...clickState, passed: false, before, after, expected };
}

async function readCartPageState(client, baseUrl) {
  await navigate(client, `${baseUrl}/cart?_qa=${Date.now()}`);
  return evaluate(
    client,
    `(() => {
      ${visibleHelper()}
      const lineItems = [...document.querySelectorAll('cart-items .cart-item')].map((item) => ({
        name: textOf(item.querySelector('.cart-item__name')),
        unitPrice: textOf(item.querySelector('.product-option')),
        total: textOf(item.querySelector('.cart-item__totals')),
        quantity: item.querySelector('.quantity__input')?.value || '',
        hasImage: Boolean(item.querySelector('.cart-item__image')),
        hasQuantityInput: isVisible(item.querySelector('.quantity__input')),
        hasPlus: Boolean(item.querySelector('.quantity__button[name="plus"]')),
        hasMinus: Boolean(item.querySelector('.quantity__button[name="minus"]')),
        hasRemove: Boolean(item.querySelector('cart-remove-button')),
        removeLabel: item.querySelector('cart-remove-button button, cart-remove-button a')?.getAttribute('aria-label') || '',
        discountText: textOf(item.querySelector('.discounts')),
        errorText: textOf(item.querySelector('.cart-item__error-text')),
      }));
      const surfaceText = textOf('main');
      const checkout = document.querySelector('#checkout');
      const additionalButtons = [...document.querySelectorAll('.cart__dynamic-checkout-buttons, .additional-checkout-buttons')]
        .filter(isVisible);
      return {
        url: location.href,
        title: document.title,
        cartVisible: isVisible('cart-items'),
        cartClasses: document.querySelector('cart-items')?.className || '',
        quantityInputDefined: Boolean(customElements.get('quantity-input')),
        cartItemsDefined: Boolean(customElements.get('cart-items')),
        emptyWarningsPresent: Boolean(document.querySelector('cart-items .cart__warnings')),
        emptyTextVisible: isVisible('cart-items .cart__empty-text'),
        continueShoppingVisible: [...document.querySelectorAll(
          'cart-items .title-wrapper-with-link a, cart-items .cart__warnings a.button'
        )].some(isVisible),
        lineItems,
        checkoutVisible: isVisible(checkout),
        checkoutDisabled: Boolean(checkout?.disabled),
        checkoutText: textOf(checkout),
        checkoutFormAction: document.querySelector('form#cart')?.action || '',
        estimatedTotal: textOf('#main-cart-footer .totals'),
        taxNote: textOf('#main-cart-footer .tax-note'),
        cartLevelDiscounts: textOf('#main-cart-footer .discounts'),
        cartErrors: textOf('#cart-errors'),
        noteVisible: isVisible('#Cart-note'),
        dynamicCheckoutButtonCount: additionalButtons.length,
        paymentIconCount: document.querySelectorAll('.footer__payment .list-payment__item').length,
        policyLinks: [...document.querySelectorAll('.policies a')].map((link) => ({
          text: textOf(link),
          href: link.href
        })),
        hasDeliveryCopy: /delivery/i.test(surfaceText),
        hasShippingCopy: /shipping/i.test(surfaceText),
        hasPickupCopy: /pickup|pick up|store pickup/i.test(surfaceText),
        hasArnhemCopy: /arnhem/i.test(surfaceText),
      };
    })()`,
  );
}

async function clickCartPageQuantityMinus(client) {
  const before = await readCartJson(client);
  const clickState = await evaluate(
    client,
    `(() => {
      const button = document.querySelector('cart-items .cart-item .quantity__button[name="minus"]');
      const input = document.querySelector('cart-items .cart-item .quantity__input');
      if (!button || button.classList.contains('disabled')) {
        return {
          clicked: false,
          buttonPresent: Boolean(button),
          buttonClasses: button?.className || '',
          buttonName: button?.name || '',
          inputValueBefore: input?.value || '',
          inputValueAfter: input?.value || '',
          quantityInputDefined: Boolean(customElements.get('quantity-input'))
        };
      }
      const inputValueBefore = input?.value || '';
      button.click();
      return {
        clicked: true,
        buttonPresent: true,
        buttonClasses: button.className || '',
        buttonName: button.name || '',
        inputValueBefore,
        inputValueAfter: input?.value || '',
        quantityInputDefined: Boolean(customElements.get('quantity-input'))
      };
    })()`,
  );
  if (!clickState.clicked) return { ...clickState, passed: false, before, after: before };
  let after = before;
  const expected = Math.max(before.itemCount - 1, 0);
  for (let attempt = 0; attempt < 8; attempt += 1) {
    after = await readCartJson(client);
    if (after.itemCount === expected) {
      await sleep(600);
      return { ...clickState, passed: true, before, after };
    }
    await sleep(250);
  }
  return { ...clickState, passed: false, before, after, expected };
}

async function clickCartPageQuantityPlus(client) {
  const before = await readCartJson(client);
  const clickState = await evaluate(
    client,
    `(() => {
      const button = document.querySelector('cart-items .cart-item .quantity__button[name="plus"]');
      const input = document.querySelector('cart-items .cart-item .quantity__input');
      if (!button || button.classList.contains('disabled')) {
        return {
          clicked: false,
          buttonPresent: Boolean(button),
          buttonClasses: button?.className || '',
          buttonName: button?.name || '',
          inputValueBefore: input?.value || '',
          inputValueAfter: input?.value || '',
          quantityInputDefined: Boolean(customElements.get('quantity-input'))
        };
      }
      const inputValueBefore = input?.value || '';
      button.click();
      return {
        clicked: true,
        buttonPresent: true,
        buttonClasses: button.className || '',
        buttonName: button.name || '',
        inputValueBefore,
        inputValueAfter: input?.value || '',
        quantityInputDefined: Boolean(customElements.get('quantity-input'))
      };
    })()`,
  );
  if (!clickState.clicked) return { ...clickState, passed: false, before, after: before };
  let after = before;
  const expected = before.itemCount + 1;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    after = await readCartJson(client);
    if (after.itemCount === expected) {
      await sleep(600);
      return { ...clickState, passed: true, before, after };
    }
    await sleep(250);
  }
  return { ...clickState, passed: false, before, after, expected };
}

async function removeCartLine(client) {
  const before = await readCartJson(client);
  const clicked = await evaluate(
    client,
    `(() => {
      const control = document.querySelector('cart-items cart-remove-button a, cart-items cart-remove-button button');
      if (!control) return false;
      control.click();
      return true;
    })()`,
  );
  if (!clicked) return { clicked, passed: false, before, after: before };
  let after = before;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    after = await readCartJson(client);
    if (after.itemCount === 0) {
      await sleep(600);
      return { clicked, passed: true, before, after };
    }
    await sleep(250);
  }
  return { clicked, passed: false, before, after, expected: 0 };
}

async function testCheckoutHandoff(client) {
  const before = await evaluate(client, "window.location.href");
  const clicked = await evaluate(
    client,
    `(() => {
      const checkout = document.querySelector('#checkout');
      if (!checkout || checkout.disabled) return false;
      checkout.click();
      return true;
    })()`,
  );
  if (!clicked) {
    return {
      clicked: false,
      before,
      after: before,
      changedUrl: false,
      external: false,
    };
  }
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const state = await evaluate(
      client,
      `(() => ({
        ready: document.readyState,
        url: window.location.href,
        title: document.title,
        bodyText: document.body?.innerText?.replace(/\\s+/g, ' ').trim().slice(0, 500) || ''
      }))()`,
    );
    if (state.url !== before) {
      const beforeUrl = new URL(before);
      const afterUrl = new URL(state.url);
      return {
        clicked: true,
        before,
        after: state.url,
        title: state.title,
        ready: state.ready,
        bodyText: state.bodyText,
        changedUrl: true,
        external: beforeUrl.origin !== afterUrl.origin,
        path: afterUrl.pathname,
      };
    }
    await sleep(250);
  }
  const after = await evaluate(client, "window.location.href");
  return {
    clicked: true,
    before,
    after,
    changedUrl: after !== before,
    external: new URL(before).origin !== new URL(after).origin,
    timedOut: true,
  };
}

async function capture(client, screenshotPath) {
  const screenshot = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  await writeFile(screenshotPath, screenshot.data, "base64");
}

function pass(status) {
  return status ? "pass" : "fail";
}

function buildFlowResult(key, label, status, details = {}) {
  return { key, label, status, details };
}

function buildIssues(flows) {
  const issues = flows
    .filter((flow) => flow.status === "fail")
    .map((flow) => ({
      code: `${flow.key}_failed`,
      severity: "error",
      detail: `${flow.label} did not meet the expected state.`,
    }));

  const firstCartPage = flows.find((flow) => flow.key === "desktop_cart_page")
    ?.details;
  const cartSurfaces = flows
    .filter((flow) => flow.key.endsWith("_cart_page") || flow.key.endsWith("_cart_drawer"))
    .map((flow) => flow.details);
  if (
    cartSurfaces.length &&
    !cartSurfaces.some((surface) => surface.hasPickupCopy || surface.hasArnhemCopy)
  ) {
    issues.push({
      code: "pickup_delivery_choice_not_clarified_before_checkout",
      severity: "recommendation",
      detail:
        "Cart drawer and cart page mention shipping/taxes but not Arnhem store pickup or the delivery-vs-pickup choice before checkout.",
    });
  }
  if (firstCartPage && firstCartPage.dynamicCheckoutButtonCount === 0) {
    issues.push({
      code: "express_checkout_not_rendered_in_local_cart_audit",
      severity: "recommendation",
      detail:
        "No additional checkout buttons were visible in the local cart-page audit; confirm on the live checkout/payment setup before launch.",
    });
  }
  if (firstCartPage && firstCartPage.policyLinks.length === 0) {
    issues.push({
      code: "legal_links_not_visible_on_cart_page",
      severity: "recommendation",
      detail:
        "No policy links were visible in the cart-page footer during the local audit.",
    });
  }
  const checkoutFlow = flows.find((flow) => flow.key === "desktop_checkout_handoff");
  if (checkoutFlow && checkoutFlow.status !== "pass") {
    issues.push({
      code: "checkout_handoff_requires_live_or_authenticated_verification",
      severity: "recommendation",
      detail:
        "The local theme preview did not reach a usable checkout page; verify checkout on the live store or an authenticated preview before changing checkout settings.",
    });
  }
  issues.push({
    code: "email_validation_requires_admin_or_test_order",
    severity: "recommendation",
    detail:
      "Abandoned-cart and confirmation emails cannot be validated from the storefront theme alone; review Shopify notification previews or test orders in Phase 7.",
  });
  return issues;
}

async function runViewportJourney(client, baseUrl, productHandle, viewport) {
  await setViewport(client, viewport.width, viewport.height, viewport.mobile);
  await clearCart(client, baseUrl);
  const add = await addProduct(client, baseUrl, productHandle);
  const cartAfterAdd = await readCartJson(client);
  const drawerBeforeQuantity = await readDrawerState(client);
  const drawerQuantityIncreased = await clickDrawerQuantityPlus(client);
  const drawerAfterQuantity = await readDrawerState(client);
  const cartAfterDrawerQuantity = await readCartJson(client);
  let cartPageQuantityIncreased = {
    skipped: true,
    passed: cartAfterDrawerQuantity.itemCount >= 2,
    before: cartAfterDrawerQuantity,
    after: cartAfterDrawerQuantity,
  };
  if (cartAfterDrawerQuantity.itemCount < 2) {
    await readCartPageState(client, baseUrl);
    cartPageQuantityIncreased = await clickCartPageQuantityPlus(client);
  }
  const cartPageBeforeDecrease = await readCartPageState(client, baseUrl);
  const quantityDecreased = await clickCartPageQuantityMinus(client);
  const cartPageAfterDecrease = await readCartPageState(client, baseUrl);
  const cartAfterDecrease = await readCartJson(client);

  return {
    add,
    cartAfterAdd,
    drawerBeforeQuantity,
    drawerQuantityIncreased,
    drawerAfterQuantity,
    cartAfterDrawerQuantity,
    cartPageQuantityIncreased,
    cartPageBeforeDecrease,
    quantityDecreased,
    cartPageAfterDecrease,
    cartAfterDecrease,
  };
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const reportPath = path.resolve(args.report);
  const screenshotDir = path.resolve(args.screenshotDir);
  const profilePath = path.resolve(`.tmp\\chrome-cart-checkout-${process.pid}`);
  await mkdir(path.dirname(reportPath), { recursive: true });
  await mkdir(screenshotDir, { recursive: true });
  if (!args.connectToExistingChrome) {
    await mkdir(profilePath, { recursive: true });
  }

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
    const consoleMessages = [];
    const documentResponses = [];
    const networkFailures = [];
    const cartRequests = new Map();
    const responseBodyTasks = [];
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
    client.on("Network.requestWillBeSent", (event) => {
      const url = event.request?.url || "";
      const isCartEndpoint =
        url.includes("/cart/add") ||
        url.includes("/cart/change") ||
        url.includes("/cart/clear") ||
        url.includes("/cart.js") ||
        url.includes("/cart?");
      if (!isCartEndpoint) return;
      cartRequests.set(event.requestId, {
        method: event.request.method,
        postData: event.request.postData || "",
      });
    });
    client.on("Network.responseReceived", (event) => {
      const url = event.response.url || "";
      const isCartEndpoint =
        url.includes("/cart/add") ||
        url.includes("/cart/change") ||
        url.includes("/cart/clear") ||
        url.includes("/cart.js") ||
        url.includes("/cart?");
      if (event.type !== "Document" && !isCartEndpoint) return;
      const request = cartRequests.get(event.requestId) || {};
      const responseRecord = {
        type: event.type,
        url,
        status: event.response.status,
        mimeType: event.response.mimeType,
        method: request.method || "",
        postData: request.postData || "",
      };
      documentResponses.push(responseRecord);
      if (!isCartEndpoint || event.response.status < 400) return;
      const bodyTask = (async () => {
        try {
          await sleep(100);
          const body = await client.send("Network.getResponseBody", {
            requestId: event.requestId,
          });
          responseRecord.responseBody = (body.body || "").slice(0, 2000);
        } catch (error) {
          responseRecord.responseBodyError = error.message;
        }
      })();
      responseBodyTasks.push(bodyTask);
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

    const desktop = await runViewportJourney(client, baseUrl, args.product, {
      name: "desktop",
      width: 1440,
      height: 900,
      mobile: false,
    });
    const shippingRates = await testShippingRates(client);
    await capture(client, path.join(screenshotDir, "desktop-cart-page.png"));
    const checkoutHandoff = await testCheckoutHandoff(client);
    await navigate(client, `${baseUrl}/cart`);
    const removed = await removeCartLine(client);
    const desktopEmptyCart = await readCartPageState(client, baseUrl);

    const mobile = await runViewportJourney(client, baseUrl, args.product, {
      name: "mobile",
      width: 390,
      height: 844,
      mobile: true,
    });
    await capture(client, path.join(screenshotDir, "mobile-cart-page.png"));
    const mobileRemoved = await removeCartLine(client);
    const mobileEmptyCart = await readCartPageState(client, baseUrl);
    await Promise.allSettled(responseBodyTasks);

    const flows = [
      buildFlowResult(
        "desktop_add_to_cart",
        "Desktop add to cart",
        pass(desktop.add.added && desktop.cartAfterAdd.itemCount === 1),
        {
          product: desktop.add.productState,
          cart: desktop.cartAfterAdd,
        },
      ),
      buildFlowResult(
        "desktop_cart_drawer",
        "Desktop cart drawer",
        pass(
          desktop.drawerBeforeQuantity.drawerActive &&
            desktop.drawerBeforeQuantity.lineItems.length === 1 &&
            desktop.drawerBeforeQuantity.checkoutVisible &&
            !desktop.drawerBeforeQuantity.checkoutDisabled,
        ),
        desktop.drawerBeforeQuantity,
      ),
      buildFlowResult(
        "desktop_drawer_quantity",
        "Desktop drawer quantity",
        pass(
          desktop.drawerQuantityIncreased.passed &&
            desktop.cartAfterDrawerQuantity.itemCount === 2 &&
            desktop.drawerAfterQuantity.lineItems[0]?.quantity === "2",
        ),
        {
          quantityIncreased: desktop.drawerQuantityIncreased,
          drawer: desktop.drawerAfterQuantity,
          cart: desktop.cartAfterDrawerQuantity,
        },
      ),
      buildFlowResult(
        "desktop_cart_page",
        "Desktop cart page",
        pass(
          desktop.cartPageBeforeDecrease.cartVisible &&
            desktop.cartPageBeforeDecrease.lineItems.length === 1 &&
            desktop.cartPageBeforeDecrease.checkoutVisible &&
            !desktop.cartPageBeforeDecrease.checkoutDisabled,
        ),
        desktop.cartPageBeforeDecrease,
      ),
      buildFlowResult(
        "desktop_cart_page_quantity",
        "Desktop cart page quantity",
        pass(
          desktop.cartPageQuantityIncreased.passed &&
            desktop.cartPageBeforeDecrease.lineItems[0]?.quantity === "2" &&
            desktop.quantityDecreased.passed &&
            desktop.cartAfterDecrease.itemCount === 1 &&
            desktop.cartPageAfterDecrease.lineItems[0]?.quantity === "1",
        ),
        {
          quantityIncreased: desktop.cartPageQuantityIncreased,
          quantityDecreased: desktop.quantityDecreased,
          cartPageBeforeDecrease: desktop.cartPageBeforeDecrease,
          cartPage: desktop.cartPageAfterDecrease,
          cart: desktop.cartAfterDecrease,
        },
      ),
      buildFlowResult(
        "desktop_shipping_rates",
        "Desktop shipping rates",
        pass(shippingRates.every((destination) => destination.passed)),
        { destinations: shippingRates },
      ),
      buildFlowResult(
        "desktop_checkout_handoff",
        "Desktop checkout handoff",
        checkoutHandoff.clicked &&
          checkoutHandoff.changedUrl &&
          !checkoutHandoff.after.startsWith("chrome-error://")
          ? "pass"
          : "not_applicable",
        checkoutHandoff,
      ),
      buildFlowResult(
        "desktop_remove_item",
        "Desktop remove item",
        pass(removed.passed && desktopEmptyCart.emptyTextVisible),
        {
          removed,
          emptyCart: desktopEmptyCart,
        },
      ),
      buildFlowResult(
        "mobile_add_to_cart",
        "Mobile add to cart",
        pass(mobile.add.added && mobile.cartAfterAdd.itemCount === 1),
        {
          product: mobile.add.productState,
          cart: mobile.cartAfterAdd,
        },
      ),
      buildFlowResult(
        "mobile_cart_drawer",
        "Mobile cart drawer",
        pass(
          mobile.drawerBeforeQuantity.drawerActive &&
            mobile.drawerBeforeQuantity.lineItems.length === 1 &&
            mobile.drawerBeforeQuantity.checkoutVisible &&
            !mobile.drawerBeforeQuantity.checkoutDisabled,
        ),
        mobile.drawerBeforeQuantity,
      ),
      buildFlowResult(
        "mobile_drawer_quantity",
        "Mobile drawer quantity",
        pass(
          mobile.drawerQuantityIncreased.passed &&
            mobile.cartAfterDrawerQuantity.itemCount === 2 &&
            mobile.drawerAfterQuantity.lineItems[0]?.quantity === "2",
        ),
        {
          quantityIncreased: mobile.drawerQuantityIncreased,
          drawer: mobile.drawerAfterQuantity,
          cart: mobile.cartAfterDrawerQuantity,
        },
      ),
      buildFlowResult(
        "mobile_cart_page",
        "Mobile cart page",
        pass(
          mobile.cartPageBeforeDecrease.cartVisible &&
            mobile.cartPageBeforeDecrease.lineItems.length === 1 &&
            mobile.cartPageBeforeDecrease.checkoutVisible &&
            !mobile.cartPageBeforeDecrease.checkoutDisabled,
        ),
        mobile.cartPageBeforeDecrease,
      ),
      buildFlowResult(
        "mobile_cart_page_quantity",
        "Mobile cart page quantity",
        pass(
          mobile.cartPageQuantityIncreased.passed &&
            mobile.cartPageBeforeDecrease.lineItems[0]?.quantity === "2" &&
            mobile.quantityDecreased.passed &&
            mobile.cartAfterDecrease.itemCount === 1 &&
            mobile.cartPageAfterDecrease.lineItems[0]?.quantity === "1",
        ),
        {
          quantityIncreased: mobile.cartPageQuantityIncreased,
          quantityDecreased: mobile.quantityDecreased,
          cartPageBeforeDecrease: mobile.cartPageBeforeDecrease,
          cartPage: mobile.cartPageAfterDecrease,
          cart: mobile.cartAfterDecrease,
        },
      ),
      buildFlowResult(
        "mobile_remove_item",
        "Mobile remove item",
        pass(mobileRemoved.passed && mobileEmptyCart.emptyTextVisible),
        {
          removed: mobileRemoved,
          emptyCart: mobileEmptyCart,
        },
      ),
    ];

    const issues = buildIssues(flows);
    const summary = {
      flowsChecked: flows.length,
      passed: flows.filter((item) => item.status === "pass").length,
      notApplicable: flows.filter((item) => item.status === "not_applicable").length,
      errors: issues.filter((item) => item.severity === "error").length,
      recommendations: issues.filter((item) => item.severity === "recommendation").length,
    };
    const report = {
      schemaVersion: 1,
      generatedAt: new Date().toISOString(),
      mode: "read_only_cart_checkout_browser_audit",
      baseUrl,
      product: args.product,
      summary,
      flows,
      issues,
      consoleMessages,
      documentResponses,
      networkFailures,
      screenshots: {
        desktopCartPage: path.join(screenshotDir, "desktop-cart-page.png"),
        mobileCartPage: path.join(screenshotDir, "mobile-cart-page.png"),
      },
    };
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    process.stdout.write(
      `${JSON.stringify(summary, null, 2)}\nReport: ${reportPath}\nScreenshot dir: ${screenshotDir}\n`,
    );
    process.exitCode = summary.errors ? 1 : 0;
  } finally {
    client?.close();
    if (chrome && !chrome.killed) chrome.kill();
    if (!args.connectToExistingChrome) {
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
}

run().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
