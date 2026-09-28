import { create } from "zustand";
import type { AppConfig } from "./types";

interface AppStore {
  config: AppConfig;
  setConfig: (c: AppConfig) => void;
  logs: string[];
  pushLog: (s: string) => void;
  clearLogs: () => void;
  running: boolean;
  setRunning: (b: boolean) => void;
}

export const useStore = create<AppStore>((set) => ({
  config: {
    base_url: "https://api.openai.com/v1",
    api_key: "",
    model: "gpt-4o-mini",
    temperature: 0.7,
  },
  setConfig: (config) => set({ config }),
  logs: [],
  pushLog: (s) => set((st) => ({ logs: [...st.logs, s] })),
  clearLogs: () => set({ logs: [] }),
  running: false,
  setRunning: (running) => set({ running }),
}));
