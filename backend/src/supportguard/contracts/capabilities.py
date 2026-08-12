from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from supportguard.contracts.tools import (
    ApiKeyMetadataInput,
    ApiKeyRevocationProposalInput,
    BillingRecordInput,
    EntitlementChangeProposalInput,
    IncidentImpactInput,
    KnowledgeSearchInput,
    NoArguments,
    RefundProposalInput,
    RequestTraceInput,
    RuntimeCommandInput,
    ServiceStatusInput,
    UsageInput,
)

READ_TOOL_INPUTS: Final[dict[str, type[BaseModel]]] = {
    "search_knowledge": KnowledgeSearchInput,
    "query_account": NoArguments,
    "query_subscription": NoArguments,
    "query_api_usage": UsageInput,
    "check_service_status": ServiceStatusInput,
    "query_billing_record": BillingRecordInput,
    "query_request_trace": RequestTraceInput,
    "query_api_key_metadata": ApiKeyMetadataInput,
    "query_incident_impact": IncidentImpactInput,
}

POLICY_TOOL_INPUTS: Final[dict[str, type[BaseModel]]] = {
    "propose_refund": RefundProposalInput,
    "propose_api_key_revocation": ApiKeyRevocationProposalInput,
    "propose_entitlement_change": EntitlementChangeProposalInput,
}

RUNTIME_COMMAND_INPUTS: Final[dict[str, type[BaseModel]]] = {
    "execute_refund": RuntimeCommandInput,
    "execute_api_key_revocation": RuntimeCommandInput,
    "execute_entitlement_change": RuntimeCommandInput,
}
