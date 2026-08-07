import { expect, test, type Page } from "@playwright/test";

// A multi-artefact request through the real app, end to end: the real planner
// prompt, the real per-step execution, the real artefact plumbing, and the
// real frontend rendering — in a real browser.
//
// This is the regression test for the failure that prompted the work: a
// three-artefact request came back as four generic process steps, no step was
// tasked with producing anything, and the synthesis re-rendered the
// spreadsheet as a markdown table. Content was right; the files were absent.
//
// Runs ONLY in the `chromium-code-execution` project (playwright.config.ts's
// CODE_EXEC_SPECS), which has its own backend with CODE_EXECUTION=true — the
// default stack deliberately runs with the shipped default (off).

let seq = 0;

async function askForArtefacts(page: Page, title: string) {
  // Date.now() alone collides when two tests register in the same
  // millisecond, which fails the second registration.
  seq += 1;
  const username = `e2e-wf-${Date.now()}-${seq}`;
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: "Register" }).click();
  await expect(page.getByLabel("Username")).not.toBeVisible();

  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByLabel("New conversation title").fill(title);
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();

  // Workflow mode explicitly, so this tests the artefact path rather than the
  // router's decision to reach it (that is auto-workflow's own spec).
  await page.getByLabel("Routing mode").selectOption("workflow");
  await page
    .getByLabel("Ask a question")
    .fill("Write the summary and build the spreadsheet of quarterly figures.");
  await page.getByRole("button", { name: /^Ask/ }).click();
  await expect(page.getByRole("button", { name: /^Ask/ })).toBeVisible({
    timeout: 30_000,
  });
}

test("a workflow's artefact step delivers a real file on the final message", async ({
  page,
}) => {
  await askForArtefacts(page, "Workflow artefacts");

  // The generated-files list lives inside the collapsible "Ran code" card,
  // exactly as it does for a single-shot answer.
  await page.getByText("Ran code").first().click();

  // The actual deliverable: a downloadable file, not prose describing one.
  const link = page.locator(".code-result-file-link").first();
  await expect(link).toBeVisible({ timeout: 15_000 });
  const download = await link.getAttribute("download");
  expect(download).toBeTruthy();
  const href = await link.getAttribute("href");
  expect(href).toMatch(/^data:/);
});

test("the delivered file previews inline, as it does in single-shot mode", async ({
  page,
}) => {
  await askForArtefacts(page, "Workflow preview");

  // Same rendering path as a single-shot answer: the "Ran code" transparency
  // card, then the inline .csv preview inside it.
  await page.getByText("Ran code").first().click();
  const preview = page.getByText(/^Preview: /).first();
  await expect(preview).toBeVisible({ timeout: 15_000 });
  await preview.click();
  await expect(page.getByRole("table")).toBeVisible({ timeout: 15_000 });
});

test("a later step is handed the earlier step's real file, through the whole stack", async ({
  page,
}) => {
  // The follow-on failure to the one above, and the harder one to see: the
  // step that WAS asked to produce a file produced it, then the next step —
  // told to work from that file — ran in its own sandbox, found nothing, and
  // rebuilt the data from memory with different numbers. Two attachments on
  // one message that quietly disagreed, with nothing reporting a problem.
  //
  // The stub answers "Carried forward ..." only when the earlier step's actual
  // CSV CONTENTS (a cell that exists nowhere else) are present in its own
  // request — a filename or a description of the file is not enough. That
  // answer reaches the synthesis, which is what surfaces here.
  await askForArtefacts(page, "Workflow carry-forward");

  // Scoped to the rendered message body: the same text is also announced in
  // the sr-only live region, which would make a bare getByText ambiguous.
  await expect(
    page.locator("p", { hasText: /Both artefacts agree/ }).first(),
  ).toBeVisible({ timeout: 15_000 });

  // Both files ride the same final message, as separate downloads.
  await page.getByText("Ran code").first().click();
  const links = page.locator(".code-result-file-link");
  await expect(links.first()).toBeVisible({ timeout: 15_000 });
  const names = await links.evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("download")),
  );
  expect(names).toContain("quarterly_report.csv");
  expect(names).toContain("regional_totals.csv");
});

test("the step-count label agrees between the badge and the breakdown", async ({
  page,
}) => {
  await askForArtefacts(page, "Workflow step count");

  // PART 2's convention: both count every step in the breakdown, synthesis
  // included. The badge used to say one number and the disclosure another.
  const badge = page.locator(".mode-badge").last();
  await expect(badge).toHaveText(/workflow\((\d+) steps\)/);
  const badgeText = (await badge.textContent()) ?? "";
  const badgeCount = Number(/workflow\((\d+) steps\)/.exec(badgeText)?.[1]);

  const disclosure = page.getByText(/^Workflow: \d+ step\(s\)$/).last();
  await expect(disclosure).toBeVisible();
  const discText = (await disclosure.textContent()) ?? "";
  const discCount = Number(/Workflow: (\d+) step/.exec(discText)?.[1]);

  expect(badgeCount).toBeGreaterThan(0);
  expect(badgeCount).toBe(discCount);
});
