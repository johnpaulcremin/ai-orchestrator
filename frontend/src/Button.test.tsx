import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Save } from "lucide-react";
import { Button } from "./Button";

describe("Button", () => {
  it("renders a labeled button with the primary/md classes", () => {
    render(
      <Button variant="primary" size="md">
        Save
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveClass("btn", "btn-primary", "btn-md");
  });

  it("defaults to the secondary/sm variant", () => {
    render(<Button>Cancel</Button>);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveClass("btn-secondary", "btn-sm");
  });

  it("renders an icon-only button with its aria-label as the accessible name and tooltip", () => {
    render(<Button iconOnly aria-label="Delete message" icon={<Save data-testid="icon" />} />);
    const button = screen.getByRole("button", { name: "Delete message" });
    expect(button).toHaveClass("btn-icon");
    expect(button).toHaveAttribute("title", "Delete message");
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("prefers an explicit title over the aria-label for the icon-only tooltip", () => {
    render(<Button iconOnly aria-label="Delete message" title="Delete" />);
    expect(screen.getByRole("button", { name: "Delete message" })).toHaveAttribute(
      "title",
      "Delete",
    );
  });

  it("does not render children text for an icon-only button even if passed", () => {
    render(<Button iconOnly aria-label="Copy" icon={<Save data-testid="icon" />} />);
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("calls onClick when clicked", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("respects the disabled prop", () => {
    render(<Button disabled>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();
  });

  it("merges a custom className alongside the generated btn classes", () => {
    render(<Button className="theme-toggle">Theme</Button>);
    expect(screen.getByRole("button", { name: "Theme" })).toHaveClass("btn", "theme-toggle");
  });
});
