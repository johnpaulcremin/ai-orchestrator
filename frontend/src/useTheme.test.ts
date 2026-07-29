import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useTheme } from "./useTheme";

const THEME_STORAGE_KEY = "ai_workbench_theme";

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(cleanup);

describe("useTheme", () => {
  it("defaults to system when nothing is stored", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("restores a previously saved light/dark choice", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("dark");
  });

  it("ignores a corrupt stored value and falls back to system", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "purple");
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("system");
  });

  it("applies a data-theme attribute and persists an explicit choice", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current[1]("dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("clears the attribute and storage when switched back to system", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current[1]("light"));
    act(() => result.current[1]("system"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it("supports the functional updater form", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current[1]("light"));
    act(() => result.current[1]((current) => (current === "light" ? "dark" : "system")));
    expect(result.current[0]).toBe("dark");
  });
});
