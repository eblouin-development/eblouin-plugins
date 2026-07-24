import { afterEach, describe, expect, it, vi } from "vitest";
import { configureApiClient, customFetch } from "./mutator";

describe("customFetch", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    // Reset module-level config so a baseUrl set by one test can't leak
    // into the next — configureApiClient replaces the config wholesale.
    configureApiClient({ baseUrl: "" });
  });

  it("resolves { data, status, headers } for a JSON response", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await customFetch<{ data: { status: string }; status: number }>("/health");

    expect(result.status).toBe(200);
    expect(result.data).toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/health",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("resolves non-2xx responses instead of throwing, for documented error responses", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: [] }), {
          status: 422,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await customFetch<{ data: unknown; status: number }>("/items", {
      method: "POST",
      body: JSON.stringify({}),
    });

    expect(result.status).toBe(422);
    expect(result.data).toEqual({ detail: [] });
  });

  it("sets a default Content-Type when a body is present, without clobbering caller-supplied headers", async () => {
    // Typed against `typeof fetch` (rather than an arg-less arrow) so
    // `.mock.calls` infers the real [url, init] tuple below.
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response("plain text", { status: 200, headers: { "content-type": "text/plain" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await customFetch("/items", {
      method: "POST",
      body: JSON.stringify({ name: "widget" }),
      headers: { Authorization: "Bearer token" },
    });

    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    const [, options] = call!;
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer token");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("does not set Content-Type for a bodyless request (e.g. GET)", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response(null, { status: 204 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await customFetch("/health", { headers: { Authorization: "Bearer token" } });

    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    const [, options] = call!;
    const headers = new Headers(options?.headers);
    expect(headers.get("Authorization")).toBe("Bearer token");
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("defaults to same-origin relative URLs when unconfigured", async () => {
    const fetchMock = vi.fn(
      async () => new Response(null, { status: 204 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await customFetch("/health");

    expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything());
  });

  it("prefixes requests with the configured baseUrl, trimming a trailing slash", async () => {
    configureApiClient({ baseUrl: "https://api.example.com/" });

    const fetchMock = vi.fn(
      async () => new Response(null, { status: 204 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await customFetch("/health");

    expect(fetchMock).toHaveBeenCalledWith("https://api.example.com/health", expect.anything());
  });

  describe("access-token injection (getAccessToken seam)", () => {
    const headersOf = (fetchMock: ReturnType<typeof vi.fn>) => {
      const call = fetchMock.mock.calls[0];
      expect(call).toBeDefined();
      return new Headers(((call as unknown[])[1] as RequestInit).headers);
    };

    it("injects Authorization: Bearer when a getter is configured and returns a token", async () => {
      configureApiClient({ baseUrl: "", getAccessToken: () => "access-123" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/me");

      expect(headersOf(fetchMock).get("Authorization")).toBe("Bearer access-123");
    });

    it("is default-off: sends no Authorization header when no getter is configured", async () => {
      configureApiClient({ baseUrl: "" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/me");

      expect(headersOf(fetchMock).has("Authorization")).toBe(false);
    });

    it("injects nothing when the getter returns null or an empty string", async () => {
      for (const value of [null, ""] as const) {
        configureApiClient({ baseUrl: "", getAccessToken: () => value });
        const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
        vi.stubGlobal("fetch", fetchMock);

        await customFetch("/auth/me");

        expect(headersOf(fetchMock).has("Authorization")).toBe(false);
      }
    });

    it("does not clobber a caller-supplied Authorization header", async () => {
      configureApiClient({ baseUrl: "", getAccessToken: () => "from-getter" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/me", { headers: { Authorization: "Bearer caller-token" } });

      expect(headersOf(fetchMock).get("Authorization")).toBe("Bearer caller-token");
    });
  });

  describe("cookie mode (web seam)", () => {
    // Small helpers to read what the mutator actually put on the wire.
    const initOf = (fetchMock: ReturnType<typeof vi.fn>) => {
      const call = fetchMock.mock.calls[0];
      expect(call).toBeDefined();
      return (call as unknown[])[1] as RequestInit;
    };
    const headersOf = (fetchMock: ReturnType<typeof vi.fn>) =>
      new Headers(initOf(fetchMock).headers);

    it("sends credentials:'include' on every request when cookie mode is on", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/health");

      expect(initOf(fetchMock).credentials).toBe("include");
    });

    it("sends X-Auth-Mode: cookie on the login request in cookie mode", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: "a@b.com", password: "x" }),
      });

      expect(headersOf(fetchMock).get("X-Auth-Mode")).toBe("cookie");
    });

    it("echoes the csrf_token cookie as X-CSRF-Token on refresh and logout", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      // Non-HttpOnly CSRF cookie the browser would expose to document.cookie.
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123; other=1" });

      for (const path of ["/auth/refresh", "/auth/logout"]) {
        const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
        vi.stubGlobal("fetch", fetchMock);

        await customFetch(path, { method: "POST", body: JSON.stringify({ refresh_token: "" }) });

        expect(headersOf(fetchMock).get("X-CSRF-Token")).toBe("csrf-abc123");
        expect(initOf(fetchMock).credentials).toBe("include");
      }
    });

    it("URL-decodes the csrf_token cookie value before echoing it", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      vi.stubGlobal("document", { cookie: "csrf_token=a%2Fb%2Bc" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/refresh", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).get("X-CSRF-Token")).toBe("a/b+c");
    });

    it("does not echo X-CSRF-Token on non-auth paths, even in cookie mode", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).has("X-CSRF-Token")).toBe(false);
    });

    it("is a safe no-op for the CSRF echo when there is no document (SSR / React Native)", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      // No document stub — typeof document === "undefined".
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/refresh", { method: "POST", body: "{}" });

      // Still cookie mode (credentials included) but no header and no throw.
      expect(headersOf(fetchMock).has("X-CSRF-Token")).toBe(false);
      expect(initOf(fetchMock).credentials).toBe("include");
    });

    it("bearer mode (the default) sends no cookies or CSRF, and opts out of the session default at login", async () => {
      // Default config: no `mode` given.
      configureApiClient({ baseUrl: "" });
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", { method: "POST", body: "{}" });
      await customFetch("/auth/refresh", { method: "POST", body: "{}" });

      const loginInit = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit;
      const loginHeaders = new Headers(loginInit.headers);
      const refreshInit = (fetchMock.mock.calls[1] as unknown[])[1] as RequestInit;
      const refreshHeaders = new Headers(refreshInit.headers);
      // The backend's default is SESSION mode, so a native client has to
      // opt out explicitly or it gets a cookie it cannot properly store.
      expect(loginHeaders.get("X-Auth-Mode")).toBe("bearer");
      expect(refreshHeaders.has("X-CSRF-Token")).toBe(false);
      expect(refreshInit.credentials).toBeUndefined();
    });

    it("treats the legacy cookieMode flag as mode: 'cookie'", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).get("X-Auth-Mode")).toBe("cookie");
    });

    it("lets an explicit mode win over the legacy cookieMode flag", async () => {
      configureApiClient({ baseUrl: "", mode: "session", cookieMode: true });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", { method: "POST", body: "{}" });

      // Session mode declares nothing at login -- it IS the backend default.
      expect(headersOf(fetchMock).has("X-Auth-Mode")).toBe(false);
    });

    it("does not clobber a caller-supplied X-CSRF-Token header", async () => {
      configureApiClient({ baseUrl: "", cookieMode: true });
      vi.stubGlobal("document", { cookie: "csrf_token=cookie-value" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/logout", {
        method: "POST",
        body: "{}",
        headers: { "X-CSRF-Token": "caller-value" },
      });

      expect(headersOf(fetchMock).get("X-CSRF-Token")).toBe("caller-value");
    });
  });

  describe("session mode (the browser default)", () => {
    const initOf = (fetchMock: ReturnType<typeof vi.fn>, index = 0) => {
      const call = fetchMock.mock.calls[index];
      expect(call).toBeDefined();
      return (call as unknown[])[1] as RequestInit;
    };
    const headersOf = (fetchMock: ReturnType<typeof vi.fn>, index = 0) =>
      new Headers(initOf(fetchMock, index).headers);

    it("sends credentials:'include' on every request", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/health");

      expect(initOf(fetchMock).credentials).toBe("include");
    });

    it("declares no X-Auth-Mode at login, because session IS the backend default", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).has("X-Auth-Mode")).toBe(false);
    });

    it("echoes X-CSRF-Token on EVERY unsafe method, not just the auth paths", async () => {
      // THE difference from cookie mode: the session cookie is Path=/, so
      // every state-changing endpoint is a CSRF target.
      configureApiClient({ baseUrl: "", mode: "session" });
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123; other=1" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items", { method: "POST", body: "{}" });
      await customFetch("/items/1", { method: "PATCH", body: "{}" });
      await customFetch("/items/1", { method: "DELETE" });
      await customFetch("/auth/logout", { method: "POST" });

      for (let i = 0; i < 4; i += 1) {
        expect(headersOf(fetchMock, i).get("X-CSRF-Token")).toBe("csrf-abc123");
      }
    });

    it("does not echo X-CSRF-Token on safe methods", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items");

      expect(headersOf(fetchMock).has("X-CSRF-Token")).toBe(false);
    });

    it("does not echo X-CSRF-Token at login, which the backend exempts", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      vi.stubGlobal("document", { cookie: "csrf_token=csrf-abc123" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/auth/login", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).has("X-CSRF-Token")).toBe(false);
    });

    it("never sends an Authorization header, even when a token getter is configured", async () => {
      // Session mode holds no token at all -- that is the property that
      // leaves an XSS payload nothing to exfiltrate. A stray Authorization
      // header would also make it ambiguous which credential authenticated
      // the request against a backend that accepts either.
      configureApiClient({
        baseUrl: "",
        mode: "session",
        getAccessToken: () => "a-token-that-must-not-be-sent",
      });
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items");

      expect(headersOf(fetchMock).has("Authorization")).toBe(false);
    });

    it("is a safe no-op for the CSRF echo when there is no document (SSR)", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      vi.stubGlobal("document", undefined);
      const fetchMock = vi.fn(async () => new Response(null, { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items", { method: "POST", body: "{}" });

      expect(headersOf(fetchMock).has("X-CSRF-Token")).toBe(false);
    });

    it("does not clobber a caller-supplied X-CSRF-Token header", async () => {
      configureApiClient({ baseUrl: "", mode: "session" });
      vi.stubGlobal("document", { cookie: "csrf_token=cookie-value" });
      const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);

      await customFetch("/items", {
        method: "POST",
        body: "{}",
        headers: { "X-CSRF-Token": "caller-value" },
      });

      expect(headersOf(fetchMock).get("X-CSRF-Token")).toBe("caller-value");
    });
  });
});
