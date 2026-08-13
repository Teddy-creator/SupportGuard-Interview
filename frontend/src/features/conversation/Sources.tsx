/* eslint-disable react-refresh/only-export-components */

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { citationEvidenceKey } from "../../citationKeys";
import type { Citation } from "../../productTypes";
import { formatTime } from "../../presentation";

function safeMarkdownUrl(url: string): string {
  if (url.startsWith("/") && !url.startsWith("//")) return url;
  try {
    const protocol = new URL(url).protocol;
    return ["http:", "https:", "mailto:"].includes(protocol) ? url : "";
  } catch {
    return "";
  }
}

export function SafeMessage({ content }: { content: string }) {
  return (
    <div className="safe-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={safeMarkdownUrl}
        components={{
          a: ({ children, href, node, ...props }) => {
            void node;
            return href ? (
              <a
                {...props}
                href={href}
                target={href.startsWith("/") ? undefined : "_blank"}
                rel={href.startsWith("/") ? undefined : "noreferrer"}
              >
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export function CitationChip({
  citations,
  index,
  primary,
}: {
  citations: Citation[];
  index: number;
  primary: boolean;
}) {
  const [open, setOpen] = useState(false);
  const citation = citations[0];
  const label = citation.title || `来源 ${index + 1}`;
  const resourceLabel = businessResourceLabel(citation);
  const evidence = deduplicatedCitationEvidence(citations);
  return (
    <span className="citation-wrap">
      <button
        className={`evidence-chip${primary ? " citation-chip" : ""}`}
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        ▤ {label}
        {citation.version ? ` v${citation.version}` : ""}
        {resourceLabel ? ` · ${resourceLabel}` : ""}
      </button>
      {open ? (
        <span className="source-popover" role="note">
          <strong>{label}</strong>
          {resourceLabel ? <small>资源：{resourceLabel}</small> : null}
          {evidence.map((item, evidenceIndex) => (
            <span
              className="source-evidence"
              key={citationEvidenceKey(item, evidenceIndex)}
            >
              {item.section_path ? <small>{item.section_path}</small> : null}
              {item.supporting_span ? (
                <span>{item.supporting_span}</span>
              ) : null}
              {item.claim_summary ? (
                <small>支持结论：{item.claim_summary}</small>
              ) : null}
            </span>
          ))}
          {citation.source_type === "business_fact" ? (
            <small>
              观测时间：{formatTime(citation.observed_at)} ·{" "}
              {citation.freshness === "fresh" ? "当前有效" : "时效未知"}
            </small>
          ) : citation.effective_at ? (
            <small>有效时间：{formatTime(citation.effective_at)}</small>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

function businessResourceLabel(citation: Citation): string | null {
  if (citation.source_type !== "business_fact") return null;
  const identity = citation.observation_source_id ?? citation.document_id;
  if (!identity?.startsWith("billing_record:")) return null;
  const separator = identity.indexOf(":");
  const resource = separator >= 0 ? identity.slice(separator + 1) : identity;
  return resource.trim() || null;
}

export function deduplicatedCitationEvidence(
  citations: Citation[],
): Citation[] {
  const selected = new Map<string, Citation>();
  for (const citation of citations) {
    const key = [
      citation.section_path ?? "",
      citation.supporting_span ?? "",
    ].join("|");
    const existing = selected.get(key);
    if (!existing) {
      selected.set(key, citation);
      continue;
    }
    const summaries = [existing.claim_summary, citation.claim_summary]
      .filter((value): value is string => Boolean(value?.trim()))
      .flatMap((value) => value.split("；").map((item) => item.trim()))
      .filter(Boolean);
    selected.set(key, {
      ...existing,
      claim_summary: [...new Set(summaries)].join("；") || undefined,
    });
  }
  return [...selected.values()];
}

function citationDocumentIdentity(citation: Citation): string {
  return (
    citation.observation_source_id ??
    citation.document_id ??
    citation.source_locator?.document_internal_id ??
    citation.source_locator?.locator_hash ??
    citation.title ??
    "unknown-source"
  );
}

export function citationSourceKey(citation: Citation): string {
  return [
    citation.source_type ?? "knowledge",
    citationDocumentIdentity(citation),
    citation.version ?? "",
  ].join("|");
}

export function groupedCitationsFor(
  citations: Citation[],
  messageId: string,
): Citation[][] {
  const groups = new Map<string, Citation[]>();
  for (const citation of citations.filter(
    (item) => item.message_id === messageId,
  )) {
    const key = citationSourceKey(citation);
    groups.set(key, [...(groups.get(key) ?? []), citation]);
  }
  const allGroups = [...groups.values()];
  const knowledge = allGroups.filter(
    (group) => (group[0]?.source_type ?? "knowledge") === "knowledge",
  );
  const business = allGroups.filter(
    (group) => group[0]?.source_type === "business_fact",
  );
  if (!knowledge.length || !business.length) return allGroups.slice(0, 3);
  const selected = [knowledge[0], business[0]];
  const selectedKeys = new Set(
    selected.map((group) => citationSourceKey(group[0])),
  );
  const remainder = allGroups.find(
    (group) => !selectedKeys.has(citationSourceKey(group[0])),
  );
  if (remainder) selected.push(remainder);
  return selected;
}
