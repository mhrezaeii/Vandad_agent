import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vandad Agent",
  description: "پنل مدیریت عملیات و Agentهای ونداد",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="fa" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
