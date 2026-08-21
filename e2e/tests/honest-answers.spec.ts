import { expect, test, type Page } from "@playwright/test";

// The guards that exist to stop the app claiming things that are not true,
// proven through the real stack rather than against a stub orchestrator: a
// real browser, the real built frontend, real SSE, a real backend, a real
// SQLite write. Each of these was unit-tested first; what a unit test cannot
// show is that the correction actually REACHES the reader, which is the whole
// point of a correction.
//
// Both run against the default stack (no extra backend): the image-claim
// guard fires regardless of whether IMAGE_GENERATION is on — the flag only
// changes the note's advice — and research mode being unavailable IS the
// default stack, since WEB_SEARCH is unset in playwright.config.ts.

async function registerAndOpenConversation(page: Page, title: string) {
  const username = `e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: "Register" }).click();
  await expect(page.getByLabel("Username")).not.toBeVisible();

  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("New conversation title").fill(title);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

test("an answer that invents an image is contradicted in the UI", async ({ page }) => {
  await registerAndOpenConversation(page, "E2E image claim");

  // "fabricate" makes the stub answer with a real transcribed lie: it says a
  // generated image is displayed inline, when none exists (see
  // e2e/stub_provider.py's IMAGE_CLAIM_ANSWER).
  await page.getByLabel("Ask a question").fill("fabricate an answer for me");
  await page.getByRole("button", { name: /^Ask/ }).click();

  // Scoped to the transcript: the same text also lands in the off-screen
  // aria-live region that announces answers to screen readers, so an
  // unscoped locator matches twice and trips Playwright's strict mode.
  const transcript = page.locator(".messages");

  // The claim is still shown -- the correction contradicts it, it does not
  // silently delete what the model said.
  await expect(
    transcript.getByText(/displayed inline with this response/),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    transcript.getByText(/no image was generated for this answer/),
  ).toBeVisible({ timeout: 15_000 });
});

test("research mode is disabled, and says why, when web search is off", async ({
  page,
}) => {
  await registerAndOpenConversation(page, "E2E research mode");

  const globe = page.getByRole("button", { name: "Toggle research mode" });
  await expect(globe).toBeVisible();
  // WEB_SEARCH is unset for this stack, so /v1/status reports it off and the
  // control must not offer to "force a live web search" it cannot perform.
  await expect(globe).toBeDisabled();
  await expect(globe).toHaveAttribute("title", /Web search retrieval/);
});
