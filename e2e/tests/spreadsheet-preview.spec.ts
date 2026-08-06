import { expect, test, type Page } from "@playwright/test";

// The inline .xlsx/.csv preview, through the real app: a real generated file
// (e2e/stub_provider.py's container-files endpoint), downloaded and
// base64'd by the real backend, parsed by the real POST
// /v1/spreadsheet-preview, and rendered by the real MessageList into a real
// browser's layout engine.
//
// Component tests cannot cover what this is actually here for. jsdom does
// not do layout -- every width is 0 there -- so "the preview panel stays
// inside its message card", "the table scrolls inside the panel rather than
// widening it", and "nothing gains a horizontal scrollbar" are only
// checkable against a real engine. This is the regression test for the bug
// where a wide sheet pushed the panel ~2100px past the edge of the card and
// got clipped by the message column instead of scrolling in place.

const MOBILE = { width: 390, height: 844 };

async function registerAndOpenSpreadsheet(page: Page, title: string) {
  const username = `e2e-sheet-${Date.now()}`;
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: "Register" }).click();
  await expect(page.getByLabel("Username")).not.toBeVisible();

  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("New conversation title").fill(title);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  // "spreadsheet" is the stub's trigger word (SPREADSHEET_TRIGGER).
  await page.getByLabel("Ask a question").fill("Make me a spreadsheet");
  await page.getByRole("button", { name: /^Ask/ }).click();

  await expect(page.getByRole("button", { name: /^Ask/ })).toBeVisible({
    timeout: 15_000,
  });
  // Open both disclosures: the "Ran code" card, then the preview inside it.
  await page.getByText("Ran code").click();
  await page.getByText("Preview: quarterly_report.csv").click();
  await expect(page.getByRole("table")).toBeVisible({ timeout: 15_000 });
}

test("a wide generated sheet previews inside its message card and scrolls in place", async ({
  page,
}) => {
  await registerAndOpenSpreadsheet(page, "Spreadsheet preview");

  const metrics = await page.evaluate(() => {
    const card = document.querySelector(".message.assistant:last-of-type");
    const panel = document.querySelector(".spreadsheet-preview");
    const wrap = document.querySelector(".spreadsheet-preview-table-wrap");
    const messages = document.querySelector(".messages");
    const de = document.documentElement;
    return {
      cardRight: card!.getBoundingClientRect().right,
      panelRight: panel!.getBoundingClientRect().right,
      panelWidth: panel!.getBoundingClientRect().width,
      cardWidth: card!.getBoundingClientRect().width,
      wrapScrollW: wrap!.scrollWidth,
      wrapClientW: wrap!.clientWidth,
      messagesOverflows: messages!.scrollWidth > messages!.clientWidth,
      pageOverflows: de.scrollWidth > de.clientWidth,
    };
  });

  // 1px of slack for fractional layout widths.
  expect(metrics.panelRight).toBeLessThanOrEqual(metrics.cardRight + 1);
  expect(metrics.panelWidth).toBeLessThanOrEqual(metrics.cardWidth + 1);
  // The overflow is REAL and lives inside the panel's own scroller -- not
  // squeezed away, and not spilling outward.
  expect(metrics.wrapScrollW).toBeGreaterThan(metrics.wrapClientW);
  expect(metrics.messagesOverflows).toBe(false);
  expect(metrics.pageOverflows).toBe(false);
});

test("the preview states the sheet's real shape and says what it is not showing", async ({
  page,
}) => {
  await registerAndOpenSpreadsheet(page, "Spreadsheet meta");

  // The stub's CSV is 120 rows x 12 columns; the preview grid caps at 50.
  await expect(page.locator(".spreadsheet-preview-sheet")).toHaveText(
    "quarterly_report.csv",
  );
  await expect(page.locator(".spreadsheet-preview-shape")).toHaveText(
    "120 rows × 12 columns",
  );
  await expect(page.locator(".spreadsheet-preview-truncated")).toContainText(
    "Showing first 50 of 120 rows",
  );
  // The first row is a real header row, and it sticks while the body scrolls.
  const header = page.getByRole("columnheader").first();
  await expect(header).toHaveText("column_heading_0");
  const wrap = page.locator(".spreadsheet-preview-table-wrap");
  const before = await wrap.boundingBox();
  await wrap.evaluate((el) => {
    el.scrollTop = 250;
  });
  const headerBox = await header.boundingBox();
  expect(Math.abs(headerBox!.y - before!.y)).toBeLessThan(3);
});

test("the right-edge scroll affordance appears only while there is more to the right", async ({
  page,
}) => {
  await registerAndOpenSpreadsheet(page, "Spreadsheet affordance");

  const scroller = page.locator(".spreadsheet-preview-scroller");
  await expect(scroller).toHaveAttribute("data-can-scroll-right", "true");

  await page.locator(".spreadsheet-preview-table-wrap").evaluate((el) => {
    el.scrollLeft = el.scrollWidth;
  });
  await expect(scroller).not.toHaveAttribute("data-can-scroll-right", "true");
});

test("at a 390px phone viewport the preview fits and never scrolls the page sideways", async ({
  page,
}) => {
  // Registered at the default viewport, then narrowed: the auth screen's
  // Register button sits below a 390x844 fold and cannot be scrolled to
  // (tracked separately -- it is not what this test is about).
  await registerAndOpenSpreadsheet(page, "Spreadsheet mobile");
  await page.setViewportSize(MOBILE);
  await expect(page.getByRole("table")).toBeVisible();

  const metrics = await page.evaluate(() => {
    const panel = document.querySelector(".spreadsheet-preview")!;
    const wrap = document.querySelector(".spreadsheet-preview-table-wrap")!;
    const de = document.documentElement;
    return {
      panelRight: panel.getBoundingClientRect().right,
      viewport: window.innerWidth,
      wrapScrollW: wrap.scrollWidth,
      wrapClientW: wrap.clientWidth,
      pageOverflows: de.scrollWidth > de.clientWidth,
      widestCell: Math.max(
        ...[...document.querySelectorAll(".spreadsheet-preview-table td")].map(
          (c) => c.getBoundingClientRect().width,
        ),
      ),
    };
  });

  expect(metrics.panelRight).toBeLessThanOrEqual(metrics.viewport + 1);
  expect(metrics.pageOverflows).toBe(false);
  // Still scrolls horizontally WITHIN itself rather than being squeezed.
  expect(metrics.wrapScrollW).toBeGreaterThan(metrics.wrapClientW);
  // The one pathological long cell is capped, not allowed to become the
  // whole screen (12rem at this breakpoint).
  expect(metrics.widestCell).toBeLessThan(metrics.viewport);
});
