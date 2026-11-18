"use client";

import { AuthPanel } from "@/components/auth-panel";
import { Dashboard } from "@/components/dashboard";
import { PendingScreen, RejectedScreen } from "@/components/pending-screen";
import { BrandMark, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

export default function HomePage() {
  const { user, initializing } = useAuth();
  const { t } = useI18n();

  if (initializing) {
    return (
      <main className="state-shell">
        <div className="splash-card">
          <BrandMark />
          <strong>{t("appName")}</strong>
          <Spinner label={t("loading")} />
        </div>
      </main>
    );
  }

  if (!user) return <AuthPanel />;
  if (user.status === "pending_approval") return <PendingScreen />;
  if (user.status === "rejected") return <RejectedScreen />;
  return <Dashboard />;
}
