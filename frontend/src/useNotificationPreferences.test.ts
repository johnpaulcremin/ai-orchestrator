import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useNotificationPreferences } from "./useNotificationPreferences";

const NOTIFY_STORAGE_KEY = "ai_workbench_notify_enabled";
const NOTIFY_SOUND_STORAGE_KEY = "ai_workbench_notify_sound_enabled";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(cleanup);

describe("useNotificationPreferences", () => {
  it("defaults both preferences to off when nothing is stored", () => {
    const { result } = renderHook(() => useNotificationPreferences());
    expect(result.current.notifyEnabled).toBe(false);
    expect(result.current.notifySoundEnabled).toBe(false);
  });

  it("restores previously enabled preferences", () => {
    window.localStorage.setItem(NOTIFY_STORAGE_KEY, "true");
    window.localStorage.setItem(NOTIFY_SOUND_STORAGE_KEY, "true");
    const { result } = renderHook(() => useNotificationPreferences());
    expect(result.current.notifyEnabled).toBe(true);
    expect(result.current.notifySoundEnabled).toBe(true);
  });

  it("persists turning notifications on and off", () => {
    const { result } = renderHook(() => useNotificationPreferences());
    act(() => result.current.setNotifyEnabled(true));
    expect(window.localStorage.getItem(NOTIFY_STORAGE_KEY)).toBe("true");
    act(() => result.current.setNotifyEnabled(false));
    expect(window.localStorage.getItem(NOTIFY_STORAGE_KEY)).toBeNull();
  });

  it("persists turning the notification sound on and off independently", () => {
    const { result } = renderHook(() => useNotificationPreferences());
    act(() => result.current.setNotifySoundEnabled(true));
    expect(window.localStorage.getItem(NOTIFY_SOUND_STORAGE_KEY)).toBe("true");
    expect(window.localStorage.getItem(NOTIFY_STORAGE_KEY)).toBeNull();
  });

  it("supports the functional updater form", () => {
    const { result } = renderHook(() => useNotificationPreferences());
    act(() => result.current.setNotifyEnabled((current) => !current));
    expect(result.current.notifyEnabled).toBe(true);
  });
});
