import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ShortcutsHelp } from "./ShortcutsHelp";

describe("ShortcutsHelp", () => {
  it("lists the Windows/Linux key labels by default", () => {
    render(<ShortcutsHelp isMac={false} onClose={() => {}} />);

    expect(screen.getByText("Ctrl+K")).toBeInTheDocument();
    expect(screen.getByText("Alt+N")).toBeInTheDocument();
    expect(screen.getByText("Escape")).toBeInTheDocument();
    expect(screen.getByText("?")).toBeInTheDocument();
  });

  it("lists the Mac key labels when isMac is true", () => {
    render(<ShortcutsHelp isMac={true} onClose={() => {}} />);

    expect(screen.getByText("⌘K")).toBeInTheDocument();
    expect(screen.getByText("⌥N")).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ShortcutsHelp isMac={false} onClose={onClose} />);

    await user.click(screen.getByRole("button", { name: "Close keyboard shortcuts" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when the Done button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ShortcutsHelp isMac={false} onClose={onClose} />);

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ShortcutsHelp isMac={false} onClose={onClose} />);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when clicking the overlay outside the dialog", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ShortcutsHelp isMac={false} onClose={onClose} />);

    await user.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalled();
  });

  it("does not close when clicking inside the dialog", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ShortcutsHelp isMac={false} onClose={onClose} />);

    await user.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
  });
});
