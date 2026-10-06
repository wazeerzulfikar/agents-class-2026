import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SyllabusPage } from "./SyllabusPage.js";

const ISSUE = [
  "# High Five, Then Verify",
  "",
  "**Issue:** The Class Runtime · Issue 02",
  "",
  "## Highlights",
  "",
  "### Atlas, Meet Evidence",
  "",
  "01 · Brooke",
  "",
  "[![Atlas, Meet Evidence](agents2026_brooke)](https://example.edu/brooke/week02/)",
  "",
  "![Logo](javascript:alert(1))",
  "",
  "Perci helps you study \\<vision\\> with sources. [Open Brooke's build →](https://example.edu/brooke/week02/)",
  "",
  "## Last word",
  "",
  "> “Offer the thread as a lens.”",
  ">",
  "> — [Egemen, from their week 2 post](https://example.edu/egemen/)",
  "",
  "[All issues →](course://newsletter)",
  "",
  "[Not a page](course://faq)",
].join("\n");

describe("SyllabusPage", () => {
  it("renders figures, quotes, and in-app course links for a newsletter issue", () => {
    const onCourseLink = vi.fn();
    render(
      <SyllabusPage
        content={ISSUE}
        courseLinkPath={(uri) => (uri === "course://newsletter" ? "/newsletter" : null)}
        error={null}
        loading={false}
        onCourseLink={onCourseLink}
        pdfDownloadUrl="/api/v1/course/resources/asset?uri=x&asset_id=pdf"
        pdfLabel="Download issue PDF"
        pdfTitle="The Class Runtime issue"
        resolveImageSource={(source) =>
          /^[a-z0-9_]+$/.test(source) ? `/api/v1/course/resources/asset?asset_id=${source}` : null
        }
      />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "High Five, Then Verify" })).toBeInTheDocument();
    expect(screen.getByText("Issue")).toBeInTheDocument();
    const figure = screen.getByRole("img", { name: "Atlas, Meet Evidence" });
    expect(figure).toHaveAttribute("src", "/api/v1/course/resources/asset?asset_id=agents2026_brooke");
    expect(figure.closest("a")).toHaveAttribute("href", "https://example.edu/brooke/week02/");
    expect(figure.closest("figure")).toHaveClass("syllabus-figure");
    // An image whose source is neither an asset id nor https is dropped, not rendered.
    expect(screen.queryByRole("img", { name: "Logo" })).not.toBeInTheDocument();
    expect(screen.getByText(/Perci helps you study <vision> with sources\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Brooke's build →" })).toHaveAttribute(
      "target",
      "_blank",
    );
    const quote = screen.getByText("“Offer the thread as a lens.”").closest("blockquote");
    expect(quote).not.toBeNull();
    expect(screen.getByRole("link", { name: "Egemen, from their week 2 post" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download issue PDF" })).toHaveAttribute(
      "download",
      "The Class Runtime issue.pdf",
    );

    const allIssues = screen.getByRole("link", { name: "All issues →" });
    expect(allIssues).toHaveAttribute("href", "/newsletter");
    expect(allIssues).not.toHaveAttribute("target");
    fireEvent.click(allIssues);
    expect(onCourseLink).toHaveBeenCalledWith("course://newsletter");
    // A course:// link without a page is shown as text, never as a dead link.
    expect(screen.queryByRole("link", { name: "Not a page" })).not.toBeInTheDocument();
    expect(screen.getByText("Not a page")).toBeInTheDocument();
  });

  it("renders no images when the page has no image resolver", () => {
    render(<SyllabusPage content={ISSUE} error={null} loading={false} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Atlas, Meet Evidence" })).toBeInTheDocument();
  });
});
