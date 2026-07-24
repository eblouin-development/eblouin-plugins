/**
 * Root layout = app entry. It runs before any screen and is the one place to:
 *   1. configure @repo/api-client (ONCE, in BEARER mode, EXPLICITLY —
 *      `mode: "bearer"`; cookie/session mode is never enabled on native, per
 *      the auth wiring);
 *   2. mount the app-wide providers (SafeAreaProvider, React Query, AuthProvider);
 *   3. hold the top-level navigator, showing a splash while auth state resolves.
 *
 * The public/protected split is enforced in each route group's own _layout
 * (app/(auth)/_layout.tsx, app/(app)/_layout.tsx) with a <Redirect>, not here.
 */
import { configureApiClient } from "@repo/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { ActivityIndicator, StyleSheet, View } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { AuthProvider } from "../src/auth/AuthProvider";
import { useAuth } from "../src/auth/useAuth";

// Bearer mode, EXPLICITLY. The backend's default is now server-side SESSIONS
// (an HttpOnly cookie — see references/wiring/auth-end-to-end.md), which is
// the wrong posture for a native client: Expo has no cookie jar suited to it
// and a real OS-backed secret store (SecureStore) for the bearer path instead,
// which is what src/auth/authEngine.ts actually uses. `mode: "bearer"` is what
// makes login send `X-Auth-Mode: bearer`, opting OUT of that default — leaving
// it unset (as an earlier, pre-session-default version of this file did, back
// when bearer WAS the backend's only mode) would silently authenticate this
// app against a session cookie it cannot store, with `login()` returning empty
// token fields. The mutator attaches whatever Authorization header the caller
// sets (the auth engine sets `Bearer <access>`) and touches no cookies. The
// base URL is inlined from EXPO_PUBLIC_API_BASE_URL at build time; unset → ""
// (same-origin relative URLs). See the api-client README's "Auth modes".
configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "", mode: "bearer" });

const queryClient = new QueryClient();

function RootNavigator() {
  const { status } = useAuth();

  // Reading the refresh token out of SecureStore on cold start is async — show
  // a splash rather than flashing a screen we might immediately redirect away.
  if (status === "loading") {
    return (
      <View style={styles.center}>
        <ActivityIndicator accessibilityLabel="Loading" />
      </View>
    );
  }

  return <Stack screenOptions={{ headerShown: false }} />;
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <StatusBar style="auto" />
          <RootNavigator />
        </AuthProvider>
      </QueryClientProvider>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
});
