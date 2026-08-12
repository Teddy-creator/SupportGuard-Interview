import { useEffect, useState } from "react";

export type Route =
  | { page: "new" }
  | { page: "conversation"; id: string }
  | { page: "approvals"; id?: string };

export function parseRoute(): Route {
  const match = window.location.pathname.match(/^\/conversations\/([^/]+)$/);
  if (match?.[1] === "new") return { page: "new" };
  if (match) return { page: "conversation", id: decodeURIComponent(match[1]) };
  const approval = window.location.pathname.match(
    /^\/approvals(?:\/([^/]+))?$/,
  );
  if (approval)
    return {
      page: "approvals",
      id: approval[1] ? decodeURIComponent(approval[1]) : undefined,
    };
  const legacy = new URLSearchParams(window.location.search).get("ticket");
  return legacy ? { page: "conversation", id: legacy } : { page: "new" };
}

export function navigate(path: string, replace = false) {
  window.history[replace ? "replaceState" : "pushState"]({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useRoute() {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const update = () => setRoute(parseRoute());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return route;
}
