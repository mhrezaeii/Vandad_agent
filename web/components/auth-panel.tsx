"use client";

import { useState, type FormEvent } from "react";
import { ApiRequestError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import {
  Alert,
  BrandMark,
  Field,
  LocaleSwitch,
  Spinner,
  cx,
} from "@/components/ui";

type Mode = "login" | "register";

const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function AuthPanel() {
  const { t } = useI18n();
  const { login, register, busy } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const switchMode = (next: Mode) => {
    setMode(next);
    setFieldErrors({});
    setFormError(null);
    setPassword("");
    setConfirmPassword("");
  };

  const describeError = (cause: unknown): string => {
    if (cause instanceof ApiRequestError) {
      if (cause.status === 401) return t("errorCredentials");
      if (cause.status === 409) return t("errorEmailTaken");
      if (cause.code === "account_rejected") return t("rejectedBody");
      if (cause.status === 0) return t("requestFailed");
      return cause.message || t("requestFailed");
    }
    if (cause instanceof Error && cause.message) return cause.message;
    return t("requestFailed");
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const next: Record<string, string> = {};
    const cleanEmail = email.trim();
    if (mode === "register" && fullName.trim().length < 2) {
      next.fullName = t("validationName");
    }
    if (!cleanEmail) next.email = t("validationEmail");
    else if (!EMAIL_PATTERN.test(cleanEmail)) {
      next.email = t("validationEmailInvalid");
    }
    if (!password) next.password = t("validationPassword");
    else if (password.length < 8) next.password = t("validationPasswordLength");
    else if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password)) {
      next.password = t("validationPasswordMix");
    }
    if (mode === "register" && password !== confirmPassword) {
      next.confirmPassword = t("validationPasswordMismatch");
    }
    setFieldErrors(next);
    setFormError(null);
    if (Object.keys(next).length > 0) return;

    try {
      if (mode === "login") {
        await login(cleanEmail, password);
      } else {
        await register(fullName.trim(), cleanEmail, password);
      }
    } catch (cause) {
      setFormError(describeError(cause));
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-hero">
        <div className="auth-hero-glow auth-hero-glow--one" aria-hidden="true" />
        <div className="auth-hero-glow auth-hero-glow--two" aria-hidden="true" />
        <div className="auth-hero-content">
          <div className="brand-lockup">
            <BrandMark />
            <div>
              <strong>{t("appName")}</strong>
              <small>{t("appTagline")}</small>
            </div>
          </div>
          <h1>{t("authHeroTitle")}</h1>
          <p>{t("authHeroText")}</p>
          <ul className="auth-feature-list">
            {[t("authFeature1"), t("authFeature2"), t("authFeature3")].map(
              (feature) => (
                <li key={feature}>
                  <span className="feature-check" aria-hidden="true">
                    ✓
                  </span>
                  {feature}
                </li>
              ),
            )}
          </ul>
          <div className="auth-hero-visual">
            <div className="hero-orb hero-orb--large" />
            <div className="hero-orb hero-orb--small" />
            <div className="hero-grid" />
            <div className="hero-chip hero-chip--one">
              <span className="hero-chip-dot" />
              Scenario · Agent
            </div>
            <div className="hero-chip hero-chip--two">
              <span className="hero-chip-dot hero-chip-dot--violet" />
              Render · KIE
            </div>
          </div>
        </div>
      </section>

      <section className="auth-panel">
        <div className="auth-panel-top">
          <LocaleSwitch />
        </div>
        <div className="auth-card glass">
          <div className="auth-card-head">
            <BrandMark small />
            <div>
              <h2>{mode === "login" ? t("loginTitle") : t("registerTitle")}</h2>
              <p>
                {mode === "login" ? t("loginSubtitle") : t("registerSubtitle")}
              </p>
            </div>
          </div>

          <div className="segmented" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              className={cx(mode === "login" && "is-active")}
              onClick={() => switchMode("login")}
            >
              {t("loginTab")}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "register"}
              className={cx(mode === "register" && "is-active")}
              onClick={() => switchMode("register")}
            >
              {t("registerTab")}
            </button>
          </div>

          {formError ? (
            <Alert tone="error" onClose={() => setFormError(null)}>
              {formError}
            </Alert>
          ) : null}

          <form className="auth-form" onSubmit={(event) => void submit(event)} noValidate>
            {mode === "register" ? (
              <Field
                label={t("fullName")}
                htmlFor="full-name"
                error={fieldErrors.fullName}
              >
                <input
                  id="full-name"
                  name="fullName"
                  type="text"
                  autoComplete="name"
                  placeholder={t("namePlaceholder")}
                  value={fullName}
                  onChange={(event) => setFullName(event.target.value)}
                  disabled={busy}
                />
              </Field>
            ) : null}

            <Field label={t("email")} htmlFor="email" error={fieldErrors.email}>
              <input
                id="email"
                name="email"
                type="email"
                inputMode="email"
                dir="ltr"
                autoComplete="email"
                placeholder={t("emailPlaceholder")}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={busy}
              />
            </Field>

            <Field
              label={t("password")}
              htmlFor="password"
              error={fieldErrors.password}
            >
              <input
                id="password"
                name="password"
                type="password"
                dir="ltr"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                placeholder={t("passwordPlaceholder")}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
              />
            </Field>

            {mode === "register" ? (
              <Field
                label={t("confirmPassword")}
                htmlFor="confirm-password"
                error={fieldErrors.confirmPassword}
              >
                <input
                  id="confirm-password"
                  name="confirmPassword"
                  type="password"
                  dir="ltr"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  disabled={busy}
                />
              </Field>
            ) : null}

            <button type="submit" className="button button--primary button--block" disabled={busy}>
              {busy ? (
                <Spinner
                  label={mode === "login" ? t("loggingIn") : t("registering")}
                />
              ) : mode === "login" ? (
                t("login")
              ) : (
                t("register")
              )}
            </button>
          </form>

          <button
            type="button"
            className="link-button"
            onClick={() => switchMode(mode === "login" ? "register" : "login")}
            disabled={busy}
          >
            {mode === "login" ? t("switchToRegister") : t("switchToLogin")}
          </button>

          <div className="auth-notes">
            <p>
              <span className="note-icon" aria-hidden="true">
                ⓘ
              </span>
              {t("approvalNotice")}
            </p>
            <p>
              <span className="note-icon note-icon--secure" aria-hidden="true">
                ⛨
              </span>
              {t("secureNotice")}
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
