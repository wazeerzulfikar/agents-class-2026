import { useCallback, useEffect, useState } from "react";

/** Course documents shown in place of the chat, each at its own shareable path. */
export type CourseDocument = "syllabus" | "newsletter-highlights";

export interface CourseDocumentRoute {
  errorMessage: string;
  loadingMessage: string;
  path: string;
  uri: string;
}

export const COURSE_DOCUMENTS: Record<CourseDocument, CourseDocumentRoute> = {
  syllabus: {
    errorMessage: "The syllabus could not be loaded. Please try again.",
    loadingMessage: "Loading syllabus…",
    path: "/about",
    uri: "course://syllabus",
  },
  // Every issue of The Class Runtime links here (python/course_server/newsletter/render.py).
  "newsletter-highlights": {
    errorMessage: "This page could not be loaded. Please try again.",
    loadingMessage: "Loading…",
    path: "/newsletter/highlights",
    uri: "course://newsletter-highlights",
  },
};

export function documentForPath(pathname: string): CourseDocument | null {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  const match = (Object.keys(COURSE_DOCUMENTS) as CourseDocument[]).find(
    (document) => COURSE_DOCUMENTS[document].path === normalized,
  );
  return match ?? null;
}

export function urlForAboutState(
  href: string,
  open: boolean,
  document: CourseDocument = "syllabus",
): string {
  const url = new URL(href);
  if (open) {
    url.pathname = COURSE_DOCUMENTS[document].path;
  } else if (documentForPath(url.pathname) !== null) {
    url.pathname = "/";
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/**
 * Whether a course document is open, a navigator, and which document it is. Opening without a
 * document means the syllabus, which is what the About button shows.
 */
export function useAboutRoute(): [
  boolean,
  (open: boolean, document?: CourseDocument) => void,
  CourseDocument,
] {
  const [current, setCurrent] = useState<CourseDocument | null>(() =>
    documentForPath(window.location.pathname),
  );

  useEffect(() => {
    const handlePopState = () => setCurrent(documentForPath(window.location.pathname));
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = useCallback((nextOpen: boolean, nextDocument: CourseDocument = "syllabus") => {
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const nextUrl = urlForAboutState(window.location.href, nextOpen, nextDocument);
    if (nextUrl !== currentUrl) {
      window.history.pushState(window.history.state, "", nextUrl);
    }
    setCurrent(nextOpen ? nextDocument : null);
  }, []);

  return [current !== null, navigate, current ?? "syllabus"];
}
