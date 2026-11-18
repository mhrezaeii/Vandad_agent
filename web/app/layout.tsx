import type { Metadata } from "next";
import "./globals.css";
import { AuthGate } from "@/app/auth";
import { LanguageSelector } from "@/app/auth";

export const metadata: Metadata = {
  title: "Vandad Agent",
  description: "پنل مدیریت عملیات و Agentهای ونداد",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fa" dir="rtl">
      <body>
        <AuthGate>
          {children}
        </AuthGate>
      </body>
    </html>
  );
}