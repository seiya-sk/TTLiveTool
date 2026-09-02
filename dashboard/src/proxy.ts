import { NextResponse, type NextRequest } from "next/server";

// Opt-in HTTP Basic Auth for temporarily exposing the dashboard over a
// tunnel (ngrok / Cloudflare Tunnel) to remote collaborators. Protecting it
// at the app level (not relying on the tunnel tool's own auth feature)
// means it works the same regardless of which tunnel is used, and doesn't
// depend on a paid tunnel plan. Off by default: with either env var unset,
// this is a no-op and local/normal use is unaffected.
//
// Enable only by setting both DASHBOARD_BASIC_AUTH_USER and
// DASHBOARD_BASIC_AUTH_PASS (e.g. in dashboard/.env.local) before starting
// the server, and unset/remove them again once the sharing session is over.
//
// Named/filed as "proxy" (not "middleware") per Next 16's renamed
// convention -- https://nextjs.org/docs/messages/middleware-to-proxy.
export function proxy(request: NextRequest) {
  const expectedUser = process.env.DASHBOARD_BASIC_AUTH_USER;
  const expectedPass = process.env.DASHBOARD_BASIC_AUTH_PASS;

  if (!expectedUser || !expectedPass) {
    return NextResponse.next();
  }

  const authHeader = request.headers.get("authorization");
  if (authHeader?.startsWith("Basic ")) {
    const decoded = Buffer.from(authHeader.slice("Basic ".length), "base64").toString("utf-8");
    const separatorIndex = decoded.indexOf(":");
    const suppliedUser = decoded.slice(0, separatorIndex);
    const suppliedPass = decoded.slice(separatorIndex + 1);
    if (suppliedUser === expectedUser && suppliedPass === expectedPass) {
      return NextResponse.next();
    }
  }

  return new NextResponse("認証が必要です / Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="TikTok Live Dashboard"' },
  });
}

// Everything except Next.js's own static asset paths -- API routes
// (screenshots, settings, streamers) and every page are covered, since
// screenshots and event data are exactly what needs protecting.
export const config = {
  matcher: "/((?!_next/static|_next/image|favicon.ico).*)",
};
