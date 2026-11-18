"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { api, apiUrl } from "@/lib/api";

// --- Authentication State ---

type AuthUser = {
  id: string;
  full_name: string;
  email: string;
  company_id: string | null;
  status: "pending_approval" | "approved" | "rejected";
};

const AUTH_KEY = "vandad.auth";
const LANG_KEY = "vandad.lang";

const DEFAULT_LANG = "fa" as const;

export function useAuth() {
  const [user, setUser] = useState<AuthUser | null>(() => {
    const saved = localStorage.getItem(AUTH_KEY);
    return saved ? JSON.parse(saved) : null;
  });
  const [language, setLanguage] = useState<"fa" | "en">(() => {
    const saved = localStorage.getItem(LANG_KEY);
    return saved ? (saved === "en" ? "en" : "fa") : DEFAULT_LANG;
  });
  const [loadingLogin, setLoadingLogin] = useState(false);
  const [errorLogin, setErrorLogin] = useState("");
  const [loadingRegister, setLoadingRegister] = useState(false);
  const [errorRegister, setErrorRegister] = useState("");
  const [companyId, setCompanyId] = useState("");

  const login = useCallback(async (email: string, password: string, remember = false) => {
    setLoadingLogin(true);
    setErrorLogin("");
    try {
      const resp = await api<{ user: AuthUser; token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password, remember }),
      });
      const data = resp;
      setUser(data.user);
      localStorage.setItem(AUTH_KEY, JSON.stringify(data.user));
      // Set company ID from user if available
      if (data.user.company_id) {
        localStorage.setItem("vandad.companyId", data.user.company_id);
        setCompanyId(data.user.company_id);
      }
      return { success: true, user: data.user };
    } catch (e: any) {
      setErrorLogin(e.response?.data?.detail || e.message || "ورود ناموفق بود.");
      return { success: false };
    } finally {
      setLoadingLogin(false);
    }
  }, []);

  const register = useCallback(async (fullName: string, email: string, password: string) => {
    setLoadingRegister(true);
    setErrorRegister("");
    try {
      const resp = await api<{ user: AuthUser }>("/auth/register", {
        method: "POST",
        body: JSON.stringify({ full_name: fullName, email, password }),
      });
      const data = resp;
      // New accounts start as pending_approval
      setUser(data.user);
      localStorage.setItem(AUTH_KEY, JSON.stringify(data.user));
      if (data.user.company_id) {
        localStorage.setItem("vandad.companyId", data.user.company_id);
        setCompanyId(data.user.company_id);
      }
      return { success: true, user: data.user };
    } catch (e: any) {
      setErrorRegister(e.response?.data?.detail || e.message || " ثبت نام ناموفق بود.");
      return { success: false };
    } finally {
      setLoadingRegister(false);
    }
  }, []);

  const logout = useCallback(() => {
    setUser(null);
    localStorage.removeItem(AUTH_KEY);
    localStorage.removeItem("vandad.companyId");
    setCompanyId("");
  }, []);

  return {
    user, language, setLanguage,
    login, register, logout,
    loadingLogin, errorLogin,
    loadingRegister, errorRegister,
    companyId, setCompanyId,
  };
}

// --- Language & RTL/LTR helpers ---

function useRtlDirection() {
  const [isRtl, setIsRtl] = useState<boolean>(true); // Persian default
  useEffect(() => {
    const lang = localStorage.getItem(LANG_KEY) || "fa";
    setIsRtl(lang !== "en");
  }, []);
  return isRtl;
}

// --- Dashboard Gate (inlined companyId from useAuth) ---

function useDashboardGate() {
  const { user } = useAuth();
  // companyId is available from useAuth return value, inline it:
  const userCompanyId = user?.company_id || localStorage.getItem("vandad.companyId") || "";

  const canAccessDashboard = useCallback((): boolean => {
    if (!user) return false;
    if (user.status === "pending_approval") return false;
    if (user.status === "rejected") return false;
    return true;
  }, [user]);

  return { user, canAccessDashboard };
}

// --- API Extension for Auth ---

declare module "@/lib/api" {
  interface API {
    "/auth/login": Promise<{ user: AuthUser; token: string }>;
    "/auth/register": Promise<{ user: AuthUser }>;
  }
}

// --- UI Components ---

export function LanguageSelector() {
  const { language, setLanguage } = useAuth();
  const isRtl = useRtlDirection();

  const handleChange = (lang: "fa" | "en") => {
    setLanguage(lang);
    localStorage.setItem(LANG_KEY, lang);
    // Force direction switch
    if (lang === "en") {
      document.dir = "ltr";
    } else {
      document.dir = "rtl";
    }
  };

  return (
    <div className="language-selector" style={{ float: isRtl ? "right" : "left", margin: "8px 12" }}>
      <button
        onClick={() => handleChange("fa")}
        style={{
          background: language === "fa" ? "rgba(255,255,255,0.2)" : "transparent",
          border: "1px solid rgba(255,255,255,0.3)",
          borderRadius: 8,
          color: language === "fa" ? "#fff" : "#fff",
          padding: "6px 12px",
          fontFamily: "iransans, system-ui, sans-serif",
          cursor: "pointer",
          transition: "all 0.2s",
        }}
        aria-label="زبان فارسی"
      >
        fa
      </button>
      <button
        onClick={() => handleChange("en")}
        style={{
          background: language === "en" ? "rgba(255,255,255,0.2)" : "transparent",
          border: "1px solid rgba(255,255,255,0.3)",
          borderRadius: 8,
          color: language === "en" ? "#fff" : "#fff",
          padding: "6px 12px",
          fontFamily: "system-ui, sans-serif",
          cursor: "pointer",
          transition: "all 0.2s",
        }}
        aria-label="English"
      >
        en
      </button>
    </div>
  );
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const { user, canAccessDashboard } = useDashboardGate();

  if (!user) {
    return <AuthLogin />;
  }

  if (user.status === "pending_approval") {
    return <PendingApproval />;
  }

  if (user.status === "rejected") {
    return <div style={{ textAlign: "center", padding: 60, direction: useRtlDirection() ? "rtl" : "ltr" }}>
      <h2 style={{ color: "#dc2626", marginBottom: 16 }}> حساب کاربری شما رد شده است </h2>
      <p style={{ color: "#6b7280", marginBottom: 32 }}> حساب کاربری شما توسط مدیر بررسی و رد شده است. لطفا با پشتیبانی تماس بگیرید. </p>
      <button onClick={() => window.location.reload()} style={{ marginTop: 16, padding: "8px 16px", background: "#3b82f6", color: "white", border: "none", borderRadius: 6 }}>صفحه را دوباره بارگذاری کنید</button>
    </div>;
  }

  // User is approved — render the actual dashboard children
  return children;
}

export function AuthLogin() {
  const { login, loadingLogin, errorLogin } = useAuth();
  const { language, setLanguage } = useAuth();
  const isRtl = useRtlDirection();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);

  useEffect(() => {
    // First-time visitors see Persian
    if (!localStorage.getItem(LANG_KEY)) {
      setLanguage("fa");
      localStorage.setItem(LANG_KEY, "fa");
      document.dir = "rtl";
    }
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    const result = await login(email, password, remember);
    if (result.success && result.user) {
      // After login, check approval status
      if (result.user.status === "pending_approval") {
        // Show pending screen; login form will unmount
        window.location.href = "/pending-approval";
      } else {
        // Approved user — redirect to dashboard
        window.location.href = "/";
      }
    }
  };

  return (
    <div className="auth-screen" style={{ direction: isRtl ? "rtl" : "ltr" }}>
      <div className="auth-card" style={{
        maxWidth: 420,
        margin: "80px auto",
        padding: 32,
        borderRadius: 12,
        background: "rgba(17, 24, 39, 0.8)",
        backdropFilter: "blur(8px)",
        border: "1px solid rgba(100, 115, 147, 0.3)",
        color: "#f1f5f9",
      }}>
        <h2 style={{ textAlign: isRtl ? "right" : "left", marginBottom: 24, fontSize: 24 }}>
          ورود به Vandad Agent
        </h2>

        {errorLogin && (
          <div style={{ background: "#dc2626", color: "white", padding: 12, borderRadius: 8, marginBottom: 16, textAlign: isRtl ? "right" : "left" }}>
            {errorLogin}
          </div>
        )}

        <form onSubmit={handleLogin} style={{ direction: isRtl ? "rtl" : "ltr" }}>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6, fontSize: 14 }}>
              {isRtl ? "ایمیل" : "Email"}
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
                fontFamily: isRtl ? "iransans, system-ui, sans-serif" : "system-ui, sans-serif",
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6, fontSize: 14 }}>
              {isRtl ? "رمزعبور" : "Password"}
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
                fontFamily: isRtl ? "iransans, system-ui, sans-serif" : "system-ui, sans-serif",
              }}
            />
          </div>

          <div style={{ marginBottom: 20, display: "flex", justifyContent: isRtl ? "flex-end" : "flex-start", gap: 8 }}>
            <label style={{ fontSize: 12, cursor: "pointer" }}>
              {isRtl ? "ادامه داد" : "Remember me"}
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                style={{ transform: "scale(0.8)", marginRight: isRtl ? "4px" : "0" }}
              />
            </label>
            <button
              type="submit"
              style={{
                flex: 1,
                padding: "10px 16px",
                borderRadius: 6,
                border: "none",
                background: "#3b82f6",
                color: "white",
                fontFamily: isRtl ? "iransans, system-ui, sans-serif" : "system-ui, sans-serif",
                fontWeight: 500,
                cursor: "pointer",
                transition: "background 0.2s",
              }}
              disabled={loadingLogin}
            >
              {loadingLogin ? "در حال ورود…" : "ورود"}
            </button>
          </div>

          <div style={{ marginTop: 16, fontSize: 13, textAlign: isRtl ? "right" : "left" }}>
            <a href="/register" style={{ color: "#94a3b8", textDecoration: "none" }}>
              {isRtl ? "حساب کاربری ندارید؟ ثبت نام کنید" : "Don't have an account? Register"}
            </a>
            &nbsp;
            <a href="/forgot-password" style={{ color: "#94a3b8", textDecoration: "none" }}>
              {isRtl ? "بفرمودرم" : "Forgot password?"}
            </a>
          </div>
        </form>
      </div>
    </div>
  );
}

export function PendingApproval() {
  const { user, logout } = useAuth();
  const isRtl = useRtlDirection();

  return (
    <div style={{ direction: isRtl ? "rtl" : "ltr", minHeight: "100vh", background: "#0f172a", padding: 32 }}>
      <div style={{ maxWidth: 500, margin: "80px auto", padding: 32, borderRadius: 12, background: "rgba(17, 24, 39, 0.8)", border: "1px solid rgba(100, 115, 147, 0.3)" }}>
        <h2 style={{ textAlign: isRtl ? "right" : "left", color: "#f87171", marginBottom: 8 }}>
          حساب کاربری شما در انتظار تأیید است
        </h2>
        <p style={{ color: "#cbd5e1", fontSize: 15, lineHeight: 1.6, marginBottom: 24 }}>
          حساب کاربری شما جدید است و قبل از دسترسی به ویژگی‌ها باید توسط مدیر تأیید شود.
        </p>
        <div style={{ background: "rgba(30, 41, 59, 0.5)", borderRadius: 8, padding: 20, marginTop: 24, textAlign: isRtl ? "right" : "left" }}>
          <p style={{ color: "#94a3b8", fontSize: 14, marginBottom: 12 }}>
            وضعیت account: <span style={{ color: "#f87171", fontWeight: 500 }}>{user?.status}</span>
          </p>
          <p style={{ color: "#94a3b8", fontSize: 14 }}>
            یک ایمیل_confirmation onto آدرس{user?.email || "your email"} ارسال خواهد شد. یا می‌توانید با پشتیبانی ارتباط برقرا
</p>
        </div>
        <div style={{ marginTop: 32, textAlign: isRtl ? "right" : "left" }}>
          <button
            onClick={logout}
            style={{
              padding: "10px 20px",
              borderRadius: 6,
              border: "none",
              background: "#6b7280",
              color: "white",
              fontFamily: isRtl ? "iransans, system-ui, sans-serif" : "system-ui, sans-serif",
              fontSize: 14,
              cursor: "pointer",
              transition: "background 0.2s",
            }}
          >
            Exterior
          </button>
        </div>
      </div>
    </div>
  );
}