from __future__ import annotations

import re

KNOWLEDGE_CONTEXT_REFERENCE = re.compile(
    r"(?:"
    r"刚才|前面|上面|之前提到|这个|这些|那个|那些|它|它们|"
    r"这两个|两者|旧版本|旧版|新版|当前版本|前一版|那(?:么)?|"
    r"\b(?:earlier|previously|above|that|those|it|they|them|"
    r"the two|old version|previous version|current version)\b"
    r")",
    re.I,
)

SAFE_STRUCTURED_ERROR_PATH = re.compile(
    r"^(?:\$|[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*)"
    r":[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_. -]+)?$"
)
ANAPHORIC_KNOWLEDGE_FOLLOW_UP = re.compile(
    r"(?:"
    r"(?:刚才|前面|上面|此前|之前提到|前文|上述)|"
    r"^(?:那|那么)\s*(?:旧版本|旧版|前一版|之前的版本)|"
    r"(?:^|[，。！？?\s])(?:这个|这些|那个|那些|它|它们|这两个|两者)|"
    r"\b(?:earlier|previously|above|that|those|it|they|them|the two)\b"
    r")",
    re.I,
)
DISCOURSE_LEADING_KNOWLEDGE_FOLLOW_UP = re.compile(
    r"^\s*(?:(?:那么|那)\s*|(?:then|so)\b[\s,:;\-]*)",
    re.I,
)
EXPLICIT_TOPIC_IDENTIFIER = re.compile(
    r"(?:"
    r"\b[A-Za-z][A-Za-z0-9]*(?:[-_./:][A-Za-z0-9]+)+\b|"
    r"\b[A-Z][A-Z0-9]{1,}\b|"
    r"\b[vV]?\d+(?:\.\d+)+(?:[-A-Za-z0-9]*)?\b"
    r")"
)
EXPLICIT_ENGLISH_TOPIC_REMAINDER = re.compile(
    r"^(?:"
    r"what\s+about\s+(?!(?:it|that|this|them|those|these)\b)|"
    r"how\s+(?:does|do|is|are)\s+(?!(?:it|that|this|they|those|these)\b)"
    r")",
    re.I,
)
EXPLICIT_CJK_TOPIC_REMAINDER = re.compile(
    r"^(?!使用时|使用中|接下来|然后|还有|最需要|需要|应该|应当|可以|能否|是否|"
    r"怎么|如何|为什么|哪些|有什么|请|再)"
    r"[\u4e00-\u9fff]{2,24}(?:呢|是什么|如何|怎么|有哪些|有什么|是否|能否|可以|"
    r"支持|限制|规则|策略|要求)"
)
TERSE_HISTORICAL_CONTEXT_FOLLOW_UP = re.compile(
    r"^\s*(?:那|那么|这个|那个)?\s*"
    r"(?:旧版本|旧版|前一版|之前的版本|old version|previous version)"
    r"\s*(?:呢|怎么样|如何|有什么不同|有什么区别)?\s*[?？]?\s*$",
    re.I,
)
KNOWLEDGE_APPLICABILITY_QUESTION = re.compile(
    r"(?:"
    r"适用|是否支持|支持吗|能否|可以吗|是否可用|以哪个为准|"
    r"一样吗|相同吗|有区别吗|"
    r"区域|套餐|模型|"
    r"\b(?:apply|applicable|supported|available|same|different|which version)\b"
    r")",
    re.I,
)
SUBSCRIPTION_POLICY_OR_OPERATIONAL_REQUEST = re.compile(
    r"(?:"
    r"政策|规则|依据|条件|原因|为什么|如何|怎么|建议|方案|流程|"
    r"调整|变更|提升|降低|扩容|优化|申请|支持吗|能否|可以吗|"
    r"\b(?:policy|rule|requirement|why|how|recommend|procedure|"
    r"change|increase|decrease|upgrade|optimi[sz]e|apply|supported)\b"
    r")",
    re.I,
)
SUBSCRIPTION_CURRENT_FACT_FIELD = re.compile(
    r"(?:"
    r"并发(?:上限|额度|限制)?|每分钟请求|请求速率|速率上限|"
    r"订阅状态|当前状态|套餐|订阅级别|订阅版本|版本号|订阅编号|订阅信息|"
    r"\b(?:concurrenc(?:y|ies)|rpm|rate[ -]?limit|subscription status|"
    r"plan|tier|subscription version|subscription id|subscription details)\b"
    r")",
    re.I,
)
