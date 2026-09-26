import { describe, expect, it } from "vitest";

import { COURSE_DOCUMENTS, documentForPath, urlForAboutState } from "./aboutRoute.js";

describe("course document routes", () => {
  it("maps each document path, with or without a trailing slash", () => {
    expect(documentForPath("/about")).toBe("syllabus");
    expect(documentForPath("/about/")).toBe("syllabus");
    expect(documentForPath("/newsletter/highlights")).toBe("newsletter-highlights");
    expect(documentForPath("/newsletter/highlights/")).toBe("newsletter-highlights");
    expect(documentForPath("/newsletter")).toBeNull();
    expect(documentForPath("/")).toBeNull();
    expect(COURSE_DOCUMENTS["newsletter-highlights"].uri).toBe("course://newsletter-highlights");
  });

  it("opens a document at its path and closes any document back to the chat", () => {
    const base = "https://cognitive-agents.media.mit.edu";
    expect(urlForAboutState(`${base}/?q=1#top`, true)).toBe("/about?q=1#top");
    expect(urlForAboutState(`${base}/`, true, "newsletter-highlights")).toBe(
      "/newsletter/highlights",
    );
    expect(urlForAboutState(`${base}/newsletter/highlights`, false)).toBe("/");
    expect(urlForAboutState(`${base}/about`, false)).toBe("/");
  });
});
