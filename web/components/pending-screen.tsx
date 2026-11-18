"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import {
  Alert,
  BrandMark,
  LocaleSwitch,
  Spinner,
  StatusPill,
} from "@/components/ui";

export function PendingScreen() {
  const { user, refreshUser, logout } = useAuth();
  const { t, formatDateTime } = useI18n();
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!user) return null;

  const check = async () => {
    setChecking(true);
    setError(null);
    try {
      await refreshUser();
    } catch {
      setError(t("requestFailed"));
    } finally {
      setChecking(false);
    }
  };

  return (
    <main className="state-shell">
      <div className="state-top">
        <div className="brand-lockup">
          <BrandMark small />
          <div>
            <strong>{t("appName")}</strong>
            <small>{t("appTagline")}</small>
          </div>
        </div>
        <LocaleSwitch />
      </div>

      <section className="state-card glass">
        <div className="state-icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
            <circle cx="24" cy="24" r="20" stroke="currentColor" strokeWidth="1.5" opacity="0.5" />
            <path
              d="M24 14v10l7 4"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
        </div>
        <StatusPill status="pending" label={t("pendingStatus")} />
        <h1>{t("pendingTitle")}</h1>
        <p className="state-body">{t("pendingBody")}</p>

        <dl className="state-details">
          <div>
            <dt>{t("signedInAs")}</dt>
            <dd>{user.full_name}</dd>
          </div>
          <div>
            <dt>{t("accountEmail")}</dt>
            <dd dir="ltr">{user.email}</dd>
          </div>
          <div>
            <dt>{t("accountCreated")}</dt>
            <dd>{formatDateTime(user.created_at)}</dd>
          </div>
        </dl>

        {error ? (
          <Alert tone="error" onClose={() => setError(null)}>
            {error}
          </Alert>
        ) : null}

        <div className="state-actions">
          <button
            type="button"
            className="button button--primary"
            onClick={() => void check()}
            disabled={checking}
          >
            {checking ? (
              <Spinner label={t("pendingRefreshing")} />
            ) : (
              t("pendingRefresh")
            )}
          </button>
          <button type="button" className="button button--ghost" onClick={logout}>
            {t("pendingLogout")}
          </button>
        </div>

        <p className="state-hint">{t("pendingPolling")}</p>
      </section>
    </main>
  );
}

export function RejectedScreen() {
  const { logout } = useAuth();
  const { t } = useI18n();

  return (
    <main className="state-shell">
      <div className="state-top">
        <div className="brand-lockup">
          <BrandMark small />
          <div>
            <strong>{t("appName")}</strong>
            <small>{t("appTagline")}</small>
          </div>
        </div>
        <LocaleSwitch />
      </div>

      <section className="state-card glass state-card--danger">
        <div className="state-icon state-icon--danger" aria-hidden="true">
          <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
            <circle cx="24" cy="24" r="20" stroke="currentColor" strokeWidth="1.5" opacity="0.5" />
            <path
              d="M18 18l12 12M30 18 18 30"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
        </div>
        <StatusPill status="rejected" label={t("reject")} />
        <h1>{t("rejectedTitle")}</h1>
        <p className="state-body">{t("rejectedBody")}</p>
        <div className="state-actions">
          <button type="button" className="button button--ghost" onClick={logout}>
            {t("rejectedLogout")}
          </button>
        </div>
      </section>
    </main>
  );
}
