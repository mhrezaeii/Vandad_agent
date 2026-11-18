"use client";

import type { ReactNode } from "react";

import { AuthProvider } from "@/lib/auth";
import { LocaleProvider } from "@/lib/i18n";

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <LocaleProvider>
      <AuthProvider>{children}</AuthProvider>
    </LocaleProvider>
  );
}
