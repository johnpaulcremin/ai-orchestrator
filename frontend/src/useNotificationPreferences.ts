import { type Dispatch, type SetStateAction, useEffect, useState } from "react";

const NOTIFY_STORAGE_KEY = "ai_workbench_notify_enabled";
const NOTIFY_SOUND_STORAGE_KEY = "ai_workbench_notify_sound_enabled";

export interface NotificationPreferences {
  notifyEnabled: boolean;
  setNotifyEnabled: Dispatch<SetStateAction<boolean>>;
  notifySoundEnabled: boolean;
  setNotifySoundEnabled: Dispatch<SetStateAction<boolean>>;
}

/**
 * Persisted on/off preferences for the background-reply notification and its
 * sound, backed by localStorage. Requesting Notification permission and
 * actually firing a notification stay in App.tsx — those need the DOM
 * Notification API and per-answer state this hook has no business holding.
 */
export function useNotificationPreferences(): NotificationPreferences {
  const [notifyEnabled, setNotifyEnabled] = useState<boolean>(
    () => window.localStorage.getItem(NOTIFY_STORAGE_KEY) === "true",
  );
  const [notifySoundEnabled, setNotifySoundEnabled] = useState<boolean>(
    () => window.localStorage.getItem(NOTIFY_SOUND_STORAGE_KEY) === "true",
  );

  useEffect(() => {
    if (notifyEnabled) {
      window.localStorage.setItem(NOTIFY_STORAGE_KEY, "true");
    } else {
      window.localStorage.removeItem(NOTIFY_STORAGE_KEY);
    }
  }, [notifyEnabled]);

  useEffect(() => {
    if (notifySoundEnabled) {
      window.localStorage.setItem(NOTIFY_SOUND_STORAGE_KEY, "true");
    } else {
      window.localStorage.removeItem(NOTIFY_SOUND_STORAGE_KEY);
    }
  }, [notifySoundEnabled]);

  return { notifyEnabled, setNotifyEnabled, notifySoundEnabled, setNotifySoundEnabled };
}
