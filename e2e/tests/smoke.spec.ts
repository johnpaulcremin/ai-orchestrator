import { expect, test } from "@playwright/test";

// One end-to-end pass through the seams a mocked unit test can't reach: a
// real browser, a real built frontend served through Vite's proxy, real SSE
// over the wire, and a real JWT auth round-trip -- against a real backend
// process whose only "fake" ingredient is the model provider itself (see
// e2e/stub_provider.py). Deliberately a single, straight-line smoke test,
// not a feature-by-feature suite -- that's what the unit/component tests
// already cover.

test("register, log in, create a conversation, and get a streamed answer", async ({
  page,
}) => {
  const username = `e2e-${Date.now()}`;
  const password = "correct horse battery staple";

  await page.goto("/");

  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  // Register also logs the new account in (see App.tsx's submitAuth) --
  // no separate "Log in" click needed.
  await page.getByRole("button", { name: "Register" }).click();

  await expect(page.getByLabel("Username")).not.toBeVisible();

  await page.getByLabel("New conversation title").fill("E2E smoke test");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: "E2E smoke test" })).toBeVisible();

  await page.getByLabel("Ask a question").fill("Say hello");
  await page.getByRole("button", { name: "$ Ask" }).click();

  // exact: true disambiguates from the aria-live region, which announces
  // "Answer received: Hello from the E2E stub." for screen readers.
  await expect(
    page.getByText("Hello from the E2E stub.", { exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  await expect(page.getByRole("button", { name: "$ Ask" })).toBeVisible();
});
