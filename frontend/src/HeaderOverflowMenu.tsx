import { type ReactNode, type RefObject, useEffect, useRef } from "react";
import { MoreHorizontal } from "lucide-react";
import { Button } from "./Button";
import { useModalFocus } from "./useModalFocus";

interface PopoverProps {
  menuRef: RefObject<HTMLDivElement | null>;
  onClose: () => void;
  children: ReactNode;
}

/** Separate component so it only mounts while open -- useModalFocus's
 * mount-time effect needs a fresh mount to actually run each time the menu
 * opens (see useModalFocus.ts and Settings.tsx's identical pattern). */
function HeaderOverflowPopover({ menuRef, onClose, children }: PopoverProps) {
  useModalFocus(menuRef);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="header-overflow-menu" role="menu" ref={menuRef} tabIndex={-1}>
      {children}
    </div>
  );
}

interface HeaderOverflowMenuProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  /** Distinguishes this instance's trigger from any other on the same page
   * (Sidebar.tsx reuses this component alongside the header's own) --
   * defaults to the header's original label so that existing usage is
   * unaffected. */
  triggerLabel?: string;
}

/**
 * A reusable "⋯ More" disclosure button + popover: everything that doesn't
 * fit in an always-visible slim row lives here instead -- the header's own
 * usage holds Rename/Tags/Duplicate/Archive/Delete/Export/Summarize/
 * Bookmarks/Templates/Usage/Settings/Compare/Share (see App.css's
 * --control-h-sm-based header rework); Sidebar.tsx's holds Import/Export
 * all/Show archived/Favorites only.
 */
export function HeaderOverflowMenu({
  open,
  onOpenChange,
  children,
  triggerLabel = "More actions",
}: HeaderOverflowMenuProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onClickOutside(event: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
        onOpenChange(false);
      }
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open, onOpenChange]);

  return (
    <div className="header-overflow" ref={wrapperRef}>
      <Button
        iconOnly
        variant="ghost"
        aria-label={triggerLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        icon={<MoreHorizontal size={20} />}
        onClick={() => onOpenChange(!open)}
      />
      {open ? (
        <HeaderOverflowPopover menuRef={menuRef} onClose={() => onOpenChange(false)}>
          {children}
        </HeaderOverflowPopover>
      ) : null}
    </div>
  );
}
