import { type Dispatch, type SetStateAction, useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";

const THEME_STORAGE_KEY = "ai_workbench_theme";

/**
 * The app's light/dark/system theme preference, persisted to localStorage
 * and applied to the document. "system" leaves the theme to the OS's
 * prefers-color-scheme (the long-standing default); an explicit light/dark
 * choice is applied via a data-theme attribute, which the CSS gives higher
 * specificity than the media query so it always wins over the OS setting.
 */
export function useTheme(): [Theme, Dispatch<SetStateAction<Theme>>] {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  });

  useEffect(() => {
    if (theme === "system") {
      document.documentElement.removeAttribute("data-theme");
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      document.documentElement.setAttribute("data-theme", theme);
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    }
  }, [theme]);

  return [theme, setTheme];
}
