"use client";

import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api, readToken, TOKEN_KEY } from "@/lib/api";
import type { AuthSession, AuthUser } from "@/lib/types";

type AuthContextValue = {
  user: AuthUser | null;
  initializing: boolean;
  busy: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (
    fullName: string,
    email: string,
    password: string,
  ) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<AuthUser | null>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function persistToken(token: string | null) {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refreshUser = useCallback(async (): Promise<AuthUser | null> => {
    if (!readToken()) {
      setUser(null);
      return null;
    }
    try {
      const me = await api<AuthUser>("/auth/me");
      if (mounted.current) setUser(me);
      return me;
    } catch (error) {
      const status = (error as { status?: number }).status;
      if (status === 401 || status === 403) {
        persistToken(null);
        if (mounted.current) setUser(null);
        return null;
      }
      throw error;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const bootstrap = async () => {
      try {
        if (readToken()) await refreshUser();
      } catch {
        // Keep the token: a transient network failure should not sign the
        // user out. The auth screen reports the failure when they retry.
      } finally {
        if (!cancelled) setInitializing(false);
      }
    };
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, [refreshUser]);

  // Pending accounts are re-checked so approval unlocks the dashboard without
  // a manual reload. The server remains the authority on the status.
  useEffect(() => {
    if (user?.status !== "pending_approval") return;
    const timer = window.setInterval(() => {
      void refreshUser().catch(() => undefined);
    }, 8000);
    return () => window.clearInterval(timer);
  }, [user?.status, refreshUser]);

  const login = useCallback(async (email: string, password: string) => {
    setBusy(true);
    try {
      const session = await api<AuthSession>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      persistToken(session.token);
      if (mounted.current) setUser(session.user);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const register = useCallback(
    async (fullName: string, email: string, password: string) => {
      setBusy(true);
      try {
        const session = await api<AuthSession>("/auth/register", {
          method: "POST",
          body: JSON.stringify({
            full_name: fullName,
            email,
            password,
          }),
        });
        persistToken(session.token);
        if (mounted.current) setUser(session.user);
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [],
  );

  const logout = useCallback(() => {
    persistToken(null);
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, initializing, busy, login, register, logout, refreshUser }),
    [user, initializing, busy, login, register, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
