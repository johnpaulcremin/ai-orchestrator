import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useModalFocus } from "./useModalFocus";

afterEach(cleanup);

function Dialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);
  return (
    <div ref={dialogRef} role="dialog" aria-modal="true" tabIndex={-1}>
      <button onClick={onClose}>Close</button>
      <input aria-label="first field" />
      <input aria-label="last field" />
    </div>
  );
}

function Harness({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <div>
      <button>Trigger</button>
      {open ? <Dialog onClose={onClose} /> : null}
    </div>
  );
}

describe("useModalFocus", () => {
  it("moves focus into the dialog when it opens", async () => {
    render(<Harness open={true} onClose={() => {}} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
  });

  it("restores focus to the trigger element when the dialog closes", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Harness open={false} onClose={() => {}} />);
    const trigger = screen.getByRole("button", { name: "Trigger" });
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    rerender(<Harness open={true} onClose={() => {}} />);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));

    rerender(<Harness open={false} onClose={() => {}} />);
    expect(document.activeElement).toBe(trigger);
    void user; // unused when no interaction is needed here
  });

  it("traps Tab within the dialog, wrapping from the last focusable element to the first", async () => {
    const user = userEvent.setup();
    render(<Harness open={true} onClose={() => {}} />);

    const close = screen.getByRole("button", { name: "Close" });
    const last = screen.getByLabelText("last field");
    expect(document.activeElement).toBe(close);

    last.focus();
    await user.tab();
    expect(document.activeElement).toBe(close); // wrapped from last back to first
  });

  it("traps Shift+Tab within the dialog, wrapping from the first focusable element to the last", async () => {
    const user = userEvent.setup();
    render(<Harness open={true} onClose={() => {}} />);

    const close = screen.getByRole("button", { name: "Close" });
    const last = screen.getByLabelText("last field");
    expect(document.activeElement).toBe(close);

    await user.tab({ shift: true });
    expect(document.activeElement).toBe(last); // wrapped from first back to last
  });
});
