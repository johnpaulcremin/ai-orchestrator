import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "./Sidebar";
import type { Conversation, SearchResult } from "./types";

function makeConversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 1,
    title: "Trip planning",
    created_at: "2026-07-20 10:00:00",
    updated_at: "2026-07-20 10:00:00",
    ...overrides,
  };
}

function makeProps(overrides: Partial<ComponentProps<typeof Sidebar>> = {}) {
  return {
    todaySpend: null,
    todayCap: null,
    todayAvoidedCost: null,
    notifyEnabled: false,
    toggleNotify: vi.fn(),
    notifySoundEnabled: false,
    setNotifySoundEnabled: vi.fn(),
    theme: "system" as const,
    setTheme: vi.fn(),
    setShortcutsHelpOpen: vi.fn(),
    title: "New AI Workbench Conversation",
    setTitle: vi.fn(),
    createConversation: vi.fn(async () => {}),
    busy: false,
    importFileInputRef: { current: null },
    importConversation: vi.fn(async () => {}),
    importing: false,
    exportAllConversations: vi.fn(async () => {}),
    exportingAll: false,
    previousConversation: undefined,
    selectedConversationId: null,
    setSelectedConversationId: vi.fn(),
    searchInputRef: { current: null },
    searchQuery: "",
    setSearchQuery: vi.fn(),
    setSearchResults: vi.fn(),
    setSearching: vi.fn(),
    isMac: false,
    allTags: [],
    tagFilter: "",
    setTagFilter: vi.fn(),
    sortOrder: "recent" as const,
    setSortOrder: vi.fn(),
    showArchived: false,
    toggleShowArchived: vi.fn(async () => {}),
    favoritesOnly: false,
    setFavoritesOnly: vi.fn(),
    bulkSelectMode: false,
    toggleBulkSelectMode: vi.fn(),
    bulkSelectedIds: new Set<number>(),
    exportSelectedConversations: vi.fn(async () => {}),
    exportingSelected: false,
    bulkTagSelected: vi.fn(async () => {}),
    bulkWorking: false,
    bulkArchiveSelected: vi.fn(async () => {}),
    bulkDeleteSelected: vi.fn(async () => {}),
    searching: false,
    searchResults: [] as SearchResult[],
    selectSearchResult: vi.fn(),
    visibleConversations: [makeConversation()],
    toggleBulkSelected: vi.fn(),
    toggleFavorite: vi.fn(async () => {}),
    jwtEnabled: false,
    me: null,
    logout: vi.fn(),
    loginUsername: "",
    setLoginUsername: vi.fn(),
    loginPassword: "",
    setLoginPassword: vi.fn(),
    submitAuth: vi.fn(async () => {}),
    authBusy: false,
    registrationAllowed: true,
    authMessage: null,
    usernameInputRef: { current: null },
    authEnabled: false,
    token: "",
    setToken: vi.fn(),
    tokenInputRef: { current: null },
    mobileOpen: false,
    onCloseMobile: vi.fn(),
    ...overrides,
  };
}

describe("Sidebar", () => {
  it("renders the conversation list and selects a conversation on click", async () => {
    const user = userEvent.setup();
    const setSelectedConversationId = vi.fn();
    render(
      <Sidebar
        {...makeProps({
          visibleConversations: [makeConversation({ id: 5, title: "Trip planning" })],
          setSelectedConversationId,
        })}
      />,
    );

    await user.click(screen.getByText("Trip planning"));
    expect(setSelectedConversationId).toHaveBeenCalledWith(5);
  });

  it("calls createConversation when Create is clicked", async () => {
    const user = userEvent.setup();
    const createConversation = vi.fn(async () => {});
    render(<Sidebar {...makeProps({ createConversation })} />);

    await user.click(screen.getByRole("button", { name: "New conversation" }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(createConversation).toHaveBeenCalled();
  });

  it("calls toggleFavorite when a conversation's star is clicked", async () => {
    const user = userEvent.setup();
    const toggleFavorite = vi.fn(async () => {});
    const conversation = makeConversation({ id: 7, title: "Star me" });
    render(
      <Sidebar {...makeProps({ visibleConversations: [conversation], toggleFavorite })} />,
    );

    await user.click(screen.getByRole("button", { name: 'Favorite "Star me"' }));
    expect(toggleFavorite).toHaveBeenCalledWith(conversation);
  });

  it("gives a conversation button a real accessible name and full-title tooltip, not an empty one", () => {
    const conversation = makeConversation({
      id: 9,
      title: "A long conversation title about quarterly planning",
      message_count: 4,
    });
    render(<Sidebar {...makeProps({ visibleConversations: [conversation] })} />);

    const button = screen.getByRole("button", {
      name: "A long conversation title about quarterly planning, 4 messages",
    });
    expect(button).toHaveAttribute("title", "A long conversation title about quarterly planning");
  });

  it("marks an archived conversation's accessible name accordingly", () => {
    const conversation = makeConversation({ id: 10, title: "Old chat", archived: true });
    render(<Sidebar {...makeProps({ visibleConversations: [conversation] })} />);

    expect(screen.getByRole("button", { name: "Old chat (archived)" })).toBeInTheDocument();
  });

  it("gives the Show archived and Favorites only checkboxes real accessible names, not 'on'", async () => {
    const user = userEvent.setup();
    render(<Sidebar {...makeProps()} />);

    await user.click(screen.getByRole("button", { name: "More conversation-list actions" }));
    expect(screen.getByRole("checkbox", { name: "Show archived conversations" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Favorites only" })).toBeInTheDocument();
  });

  it("shows search results and highlights the matched term instead of the normal list", () => {
    render(
      <Sidebar
        {...makeProps({
          searchQuery: "trip",
          searchResults: [
            { ...makeConversation({ title: "Trip planning" }), snippet: "Plan a trip to Kyoto" },
          ],
        })}
      />,
    );

    expect(screen.getByText("Plan a", { exact: false })).toBeInTheDocument();
  });

  it("applies the sidebar-open class when mobileOpen is true, not otherwise", () => {
    const { container, rerender } = render(<Sidebar {...makeProps({ mobileOpen: false })} />);
    expect(container.querySelector(".sidebar")).not.toHaveClass("sidebar-open");

    rerender(<Sidebar {...makeProps({ mobileOpen: true })} />);
    expect(container.querySelector(".sidebar")).toHaveClass("sidebar-open");
  });

  it("calls onCloseMobile when the mobile close button is clicked", async () => {
    const user = userEvent.setup();
    const onCloseMobile = vi.fn();
    render(<Sidebar {...makeProps({ mobileOpen: true, onCloseMobile })} />);

    await user.click(screen.getByLabelText("Close conversation list"));
    expect(onCloseMobile).toHaveBeenCalled();
  });
});
