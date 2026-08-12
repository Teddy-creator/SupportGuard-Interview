import type { Citation } from "./productTypes";

export function citationEvidenceKey(
  citation: Citation,
  index: number,
): string {
  if (citation.claim_id) return citation.claim_id;
  const binding =
    citation.citation_binding_id ??
    citation.observation_source_id ??
    citation.document_id ??
    citation.title ??
    "citation";
  return `${binding}:${citation.message_id ?? "unbound"}:${index}`;
}
