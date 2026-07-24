import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { adminPingAdminPingGet, configureApiClient } from "@repo/api-client";
import { createQueryClient } from "../query/createQueryClient";
import { unwrap } from "../errors/unwrap";
import { __resetAuthBridge } from "./authBridge";
import { AuthProvider } from "./AuthProvider";
import { RequireRole } from "./guards";
import { useAuth } from "./useAuth";

const ORIGIN = "http://localhost";
const CSRF = "csrf-xyz";

// --- MSW server -----------------------------------------------------------
const server = setupServer();

// Per-test observations.
let loginAuthMode: string | null = null;
let logoutCsrfHeader: string | null = null;

/** Module-scoped "is there a live session" flag the handlers below share --
 * the harness's own stand-in for the backend's `sessions` table, since these
 * tests exercise the client against a fake HTTP layer (MSW), not a real
 * server-side session store. */
let sessionLive = false;

const loginHandler = () =>
  http.post(`${ORIGIN}/auth/login`, ({ request }) => {
    loginAuthMode = request.headers.get("X-Auth-Mode");
    sessionLive = true;
    // Session mode's real response shape: both token fields empty, the
    // credential is the (mocked, here nonexistent) HttpOnly cookie.
    return HttpResponse.json(
      { access_token: "", refresh_token: "", token_type: "session" },
      { status: 200 },
    );
  });

const meHandler = (roles: string[] = ["admin"]) =>
  http.get(`${ORIGIN}/auth/me`, () =>
    sessionLive
      ? HttpResponse.json({ id: "user-1", email: "user@example.com", roles }, { status: 200 })
      : unauthorized(),
  );

const logoutHandler = () =>
  http.post(`${ORIGIN}/auth/logout`, ({ request }) => {
    logoutCsrfHeader = request.headers.get("X-CSRF-Token");
    sessionLive = false;
    return new HttpResponse(null, { status: 204 });
  });

const unauthorized = () =>
  HttpResponse.json(
    { error: { code: "unauthenticated", message: "Not authenticated" } },
    { status: 401 },
  );

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());

beforeEach(() => {
  loginAuthMode = null;
  logoutCsrfHeader = null;
  sessionLive = false;
  __resetAuthBridge();
  // The non-HttpOnly csrf_token cookie the backend would have set; the
  // mutator echoes it as X-CSRF-Token on every unsafe method in session mode.
  document.cookie = `csrf_token=${CSRF}`;
  configureApiClient({ baseUrl: ORIGIN, mode: "session" });
});

afterEach(() => {
  server.resetHandlers();
  configureApiClient({ baseUrl: "" });
});

// --- harness --------------------------------------------------------------
const AdminPing = () => {
  const { isAuthenticated } = useAuth();
  const ping = useQuery({
    queryKey: ["admin-ping"],
    queryFn: async () => unwrap(await adminPingAdminPingGet()),
    enabled: isAuthenticated,
    retry: false,
  });
  return <div data-testid="ping">{ping.isSuccess ? "ping-ok" : "ping-pending"}</div>;
};

const Harness = () => {
  const auth = useAuth();
  return (
    <div>
      <button onClick={() => void auth.login("user@example.com", "pw").catch(() => {})}>
        login
      </button>
      <button onClick={() => void auth.logout()}>logout</button>
      <div data-testid="authed">{String(auth.isAuthenticated)}</div>
      {auth.principal ? <div data-testid="email">{auth.principal.email}</div> : null}
      <RequireRole role="admin" fallback={<div data-testid="denied">denied</div>}>
        <div data-testid="admin-area">admin area</div>
      </RequireRole>
      <AdminPing />
    </div>
  );
};

const renderApp = (opts?: { onAuthExpired?: () => void }) => {
  const queryClient = createQueryClient();
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider onAuthExpired={opts?.onAuthExpired}>
          <Harness />
        </AuthProvider>
      </QueryClientProvider>,
    ),
  };
};

describe("AuthProvider — session-mode lifecycle", () => {
  it("login sends no X-Auth-Mode header, holds no token, and surfaces the /auth/me principal + roles", async () => {
    server.use(
      loginHandler(),
      meHandler(),
      http.get(`${ORIGIN}/admin/ping`, () => HttpResponse.json({ status: "ok" }, { status: 200 })),
    );
    const user = userEvent.setup();
    renderApp();

    expect(screen.getByTestId("authed")).toHaveTextContent("false");
    expect(screen.getByTestId("denied")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "login" }));

    await waitFor(() => expect(screen.getByTestId("authed")).toHaveTextContent("true"));
    // session mode declares nothing at login -- it IS the backend default.
    expect(loginAuthMode).toBeNull();
    // principal surfaced from /auth/me, including roles (there is no JWT
    // for this component to decode a claim out of in session mode).
    expect(await screen.findByTestId("email")).toHaveTextContent("user@example.com");
    expect(screen.getByTestId("admin-area")).toBeInTheDocument();
    // the previously-401 ping succeeds once the cookie (mocked here via
    // sessionLive) authenticates it.
    await waitFor(() => expect(screen.getByTestId("ping")).toHaveTextContent("ping-ok"));
  });

  it("a user with no admin role does not see the admin area", async () => {
    server.use(loginHandler(), meHandler([]));
    const user = userEvent.setup();
    renderApp();

    await user.click(screen.getByRole("button", { name: "login" }));

    await waitFor(() => expect(screen.getByTestId("authed")).toHaveTextContent("true"));
    expect(screen.getByTestId("denied")).toBeInTheDocument();
  });

  it("logout echoes the CSRF header, revokes the session, and clears the principal", async () => {
    server.use(loginHandler(), meHandler(), logoutHandler());
    const user = userEvent.setup();
    renderApp();

    await user.click(screen.getByRole("button", { name: "login" }));
    await waitFor(() => expect(screen.getByTestId("authed")).toHaveTextContent("true"));

    await user.click(screen.getByRole("button", { name: "logout" }));

    expect(logoutCsrfHeader).toBe(CSRF);
    await waitFor(() => expect(screen.getByTestId("authed")).toHaveTextContent("false"));
    expect(screen.queryByTestId("email")).not.toBeInTheDocument();
  });

  it("a 401 from a non-auth call notifies session-invalidated and fires onAuthExpired -- no refresh is attempted", async () => {
    const onAuthExpired = vi.fn();
    server.use(
      loginHandler(),
      meHandler(),
      // The session dies server-side (e.g. revoked in another tab) between
      // login and this request -- there is no /auth/refresh to fall back to
      // on the session path, unlike the JWT paths this replaced. Flips the
      // shared `sessionLive` flag so the SUBSEQUENT /auth/me refetch this
      // provider triggers also reflects the dead session, matching a real
      // backend (one session store, consistent across every endpoint).
      http.get(`${ORIGIN}/admin/ping`, () => {
        sessionLive = false;
        return unauthorized();
      }),
    );
    const user = userEvent.setup();
    renderApp({ onAuthExpired });

    await user.click(screen.getByRole("button", { name: "login" }));

    // login → ping 401 → session-invalidated notification → principal
    // cleared + onAuthExpired. The whole cycle can complete before the
    // first assertion runs, so assert only the terminal state, not the
    // transient logged-in moment (same race the original bearer-mode
    // version of this test already had to account for). No /auth/refresh
    // call exists in this suite's handlers at all -- if the provider tried
    // to call one, MSW's onUnhandledRequest: "error" would fail the test.
    await waitFor(() => expect(onAuthExpired).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("authed")).toHaveTextContent("false"));
  });
});
