# SupportGuard Interview Edition Simplification v2.0

- 文档类型：重大范围收敛、权威迁移与结构简化提案
- 基线仓库：私有 canonical repository（公开镜像已对本机绝对路径做最小脱敏）
- 基线提交：`6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb`
- 状态：已批准并冻结；用户已于 2026-08-11 明确授权执行 Phase 0～7；Phase 0～6 已完成；Phase 7 首个 Candidate `b132c395c2edf2d7d72477dc9051bffc3d7f4024` 的一次性 IE-P16 为 `11/16`；replacement `7527c0acca079f57549538e49135a91ef87b9389` 通过全部前置证明和 Hosted CI，但一次性 IE-P16 为 `13/16`；两个 SHA 均已消费且不得重跑，用户已于 2026-08-13 持续授权后续 clean Candidate 的必要真实 DeepSeek 验证，当前继续通用超时诊断与修复；最终 Definition of Done 未完成
- 目标读者：项目作者、AI 应用 / Agent 开发岗位面试官、后续开发 Agent

## 1. 决策摘要

SupportGuard 已具备真实的单 Agent Tool-use、RAG、MCP、HITL、Memory、异步执行、多租户和
审计链路，但当前仓库的工程量、历史治理层和核心模块尺寸已经超过面试项目的合理认知预算。
本提案建立一个可由项目作者真正掌握、能在 15～30 分钟内解释、同时保留完整 Agent 纵向价值
的 **Interview Edition**。

本轮是“行为保持下的认知与结构收敛”，不是继续堆功能，也不是把系统降级为聊天玩具。精简
对象是重复实现、不可达能力、历史验收基础设施、默认阅读噪声和生产镜像中的开发工具；
Tenant、RLS、Policy、HITL、Idempotency、Effect-once、Citation 和 Provider fail-closed
不得因精简而退化。

最终版本必须同时满足：

1. 用户能够完成三条核心旅程；
2. 面试官能沿总计不超过 12 个入口追踪完整请求；
3. 项目作者的“必须掌握核心”目标为 6,000～8,000 行、绝对上限 12,000 行，并能解释每个
   阶段的输入、输出和失败方式；
4. LLM 永远不能越过确定性 Policy、租户边界和人工审批直接执行高风险动作；
5. 完整历史可验证、可恢复，但不再占据默认工作区、运行时包或面试阅读路径。

## 2. 为什么必须收敛

基线静态盘点：

- 后端生产 Python 约 `94,610` 行，后端测试约 `96,437` 行；
- `acceptance + evidence + evals + diagnostics` 约 `35,774` 行，占生产 Python 包约 `38%`；
- `188` 个 Alembic Migration、`1,552` 个已跟踪 `evals/reports` 文件；
- `runtime_support.py=2,948` 行、`action_flow.py=1,896` 行、
  `decision_support.py=1,865` 行；
- 前端 `useConversationController.ts=702` 行；
- `AGENTS.md` 同时承担操作宪法、历史状态账本和多代执行授权。

这些数字已经产生三种真实风险：

- **所有权风险**：项目作者无法在随机追问下解释局部实现；
- **可信度风险**：历史补丁、Gate 和兼容层会让面试官怀疑代码是否真正由作者掌握；
- **变更风险**：局部语义调整可能触发 Graph、持久化、前端和大批历史测试的连锁修改。

v2.0 不以总行数最小为目标，而以 owner 唯一、依赖短、核心可读、测试可定位和作者能掌握为
目标。

## 3. 激活条件与权威迁移

### 3.1 两步激活

用户“批准本文档”只授权一个 **docs-only Authority Commit**：

1. 将本文状态改为“已批准并冻结”；
2. 将 `AGENTS.md` 改为一页当前操作宪法；
3. 写入下表所列权威迁移和继承规则；
4. 不修改 Runtime、测试、Migration、历史证据或远程仓库结构。

只有用户随后下达明确的 v2.0 执行 Prompt，才授权 Phase 0～7。执行 Prompt 应明确授权创建
Archive annotated tag；否则 Phase 0 在创建 Tag 前暂停。批准文档不等于批准删除默认分支文件、
替换 Migration、运行真实 Provider 或改变远程仓库。

### 3.2 规则优先级

v2.0 激活后，冲突按以下顺序处理：

1. 本文明确条款；
2. 本节明确继承的安全不变量；
3. 当前精简后的 `AGENTS.md` 操作规则；
4. 历史文档与 Receipt 只用于事实追溯，不再授权执行。

### 3.3 Authority Transition

| 现有材料 | v2.0 后身份 | 继续生效的内容 | 验证方式 |
| --- | --- | --- | --- |
| `production-hardening-v1.2.md`、`interview-mvp-v1.md` | Archive safety source | LLM/MCP 无 Mutation、Policy/HITL、Tenant、幂等、事务重校验 | `safety-invariant-manifest.json` 映射到新测试 |
| v1.2.1～v1.5.32 Addendum、失败 Baseline、Receipt | 不可改写历史证据 | 原 SHA、分数、失败、费用、Hash 和禁止重跑事实 | Archive 文件 Hash Manifest + 恢复演练 |
| v1.5.12 产品范围 | 被 v2.0 展示范围取代 | 三类 Action、安全边界、多轮和审批语义 | v2.0 Journey / Action Contract |
| v1.6、v1.7 | 结构与性能基线 | 已验证行为和历史 `37/37` 的准确归属 | Characterization + 历史 Receipt 链接 |
| 当前 `AGENTS.md` | 被 docs-only Authority Commit 替换 | Secret、费用、不可逆操作、用户修改保护、禁止受保护评测 | 新 `AGENTS.md` 自检 |
| Evaluation v6、Holdout、Cross-Encoder | 保持未执行 / 禁止访问 | 不得调参、运行或宣称结果 | Release Verification 明示 `not executed` |
| 188 个 Migration | Archive 历史 | 最终 Schema 的权限与约束结果 | Schema Equivalence Manifest |

Archive 前必须生成逐文件 SHA-256 Manifest，覆盖 Migration、Receipt、Matrix、Manifest、Prompt
和语料。任何历史结果只允许复制与校验，不允许重写为新版本通过证据。

## 4. 产品定位与面试表面

定位保持不变：**面向 AI SaaS 客服的、证据优先且可安全执行高风险动作的单 Agent 系统**。

### 4.1 三条核心 Demo Contract

| Demo | 身份与输入 | 必需证据 / 路径 | 页面终态 |
| --- | --- | --- | --- |
| 429 诊断 | `cust_demo / tenant_demo`；“余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded，为什么？” | Native Decision；Knowledge + 当前订阅 / 用量 Observation；引用绑定；不得把 429 误判为余额不足 | 中文解释、下一步、知识引用、实时事实、零 Action |
| 重复扣费退款 | 同一客户；`bill_demo_duplicate` 是 `bill_demo_original` 的 49 USD 重复扣费，请按政策退款 | Billing + Policy Evidence；ActionCandidate；Approval Snapshot；Approver approve；Resume；Runtime-only Effect | 客户可继续聊天；审批后仅执行一次；账单与审计终态一致 |
| 越权拦截 | `cust_demo / tenant_demo` 在普通输入中引用 `tenant_other` 的 `bill_other_001` 并要求直接退款 | 可信 Scope；Read Tool 不返回跨租户事实；零 Proposal / Approval / Effect；响应不可泄露资源存在性 | 客户看到安全、可行动的范围说明；技术视图显示确定性拒绝 |

三条 Demo 均从统一 Web 入口完成，不增加 Demo-only 路由、Case ID 或固定文本分支。真实面试模式
使用 DeepSeek Native Tool Calling；确定性测试和离线 UI 使用 Fake Provider。Provider 外部不可用
时必须 fail closed，并将该次演示标记为环境阻塞而不是伪造成功。

### 4.2 五条备用场景

1. 已退款账单不得重复创建申请；
2. API Key 撤销必须形成审批并 effect-once；
3. Entitlement Change 的 quota change 必须形成审批并 effect-once，plan change 合同继续保留；
4. 事故、账户或订阅事实过期时重新查询或给出有限结论；
5. 问候、身份询问、范围外问题和缺少资源引用时给出自然且可行动的回答。

退款、API Key 撤销和 Entitlement Change 都保留真实端到端 Proposal、Approval 和 Runtime Effect，
但必须复用一套 `ActionSpec → Approval → RuntimeEffect` 实现。项目作者只需掌握一次通用流程，
再解释三份类型化 ActionSpec 的字段与事务前置条件。

## 5. 保留能力与明确删减

### 5.1 必须保留

- 一个显式 LangGraph 和真实 `Decision → Read Tool → Observation → Replan / Stop` 有界循环；
- PostgreSQL 权威状态、RLS、Redis Streams、Outbox、Lease / Fence 和 Worker；
- 两个真实 stdio MCP Server；
- Markdown Hybrid Retrieval、RRF、Evidence Selection、Citation Binding、版本冲突与拒答；
- 三类 Action 的确定性 Policy、Approval Snapshot、Checkpoint / Resume、事务重校验和 Effect-once；
- 非权威结构化 Memory、Freshness 和客户安全 Timeline；
- Conversation-first 客户界面与独立审批者界面。

### 5.2 MCP 权限矩阵

| 工具面 | 工具 | 谁可调用 | 权限 |
| --- | --- | --- | --- |
| Read MCP | `search_knowledge`、`query_account`、`query_subscription`、`query_api_usage`、`check_service_status`、`query_billing_record`、`query_request_trace`、`query_api_key_metadata`、`query_incident_impact` | Agent 在每轮 Allowlist 内看到 0～9 个 | 只读 Observation |
| Proposal MCP | `propose_refund`、`propose_api_key_revocation`、`propose_entitlement_change` | 仅确定性 Policy | 惰性 Proposal，不产生业务 Effect |
| Runtime | 三类 Execute Capability | 仅审批后 Runtime Finalizer | 事务 Mutation |

当前不可达且没有 Operator Inbox 承接的 `create_support_escalation` 从 v2.0 MCP Schema 和 Graph
中删除；历史调用与 Schema Hash 仅在 Archive 保留。这个删除不等于实现人工坐席系统。

MCP 必须继续验证 initialize、list_tools、Schema Hash、Allowlist、超时、有界重连、限时关闭、
权限隔离和零孤儿进程。

### 5.3 不进入 Interview Edition

- Operator Inbox、坐席分配、SLA、人工回复闭环；
- 完整 IAM 后台、MFA、SSO、SCIM；
- 真实支付、Key 管理和配额供应商；
- 多 Agent、开放式 ReAct、动态模型路由；
- Milvus、PDF / OCR / VLM、第二套队列；
- 默认在线 Cross-Encoder；
- 生产级备份、PITR、跨区域 DR、SBOM 签名；
- 新的受保护 Evaluation v6 Holdout 或历史 Gate / Parity。

## 6. 必须掌握核心与代码结构

### 6.1 总计 12 个默认入口

```text
 1. api/messages.py                 # 接受消息、幂等和查询入口
 2. agent/graph.py                  # 唯一 LangGraph Composition Root
 3. agent/decision.py               # Provider Decision 与结构校验
 4. agent/tool_loop.py              # MCP、Observation、预算与 Replan
 5. agent/evidence.py               # Evidence Requirement、Claim、Citation
 6. agent/policy.py                 # 纯确定性 Publication / Action Policy
 7. actions/service.py              # ActionSpec、Approval、Resume、Effect
 8. rag/service.py                  # Retrieve → Fuse → Select → Bind
 9. mcp/runtime.py                  # 两个 Server 的会话和恢复
10. runtime/worker.py               # Outbox、Redis、Lease、Worker
11. db/security_contract.py         # Baseline、RLS、Grant、Capability、约束
12. frontend/src/App.tsx            # 客户 / 审批者产品流入口
```

这是整个面试 Code Map 的总数，不是“后端 12 + 前端另计”。每个入口最多标记两个一跳必读依赖；
`App.tsx` 的两个一跳必读依赖固定为 `ConversationPage.tsx` 与 `ApprovalPage.tsx`。核心业务判断
不得藏入名为 `utils`、Repository 或通用 helper 的模块。

三条 Demo 的 Owner Map 固定为：

- 429：`1 → 10 → 2 → 3 → 4 → 9 → 8 → 5 → 6 → 12`；
- 退款：`1 → 10 → 2 → 3 → 4 → 9 → 8 → 5 → 6 → 7 → 11 → 12`；
- 越权拦截：`1 → 10 → 2 → 3 → 4 → 9 → 6 → 11 → 12`。

它们可以使用 12 个入口中的真实子集，不再用人为的“单 Demo ≤8 文件”隐藏 Queue、数据库或 UI。
Owner Map 中的编号必须在 `interview-guide.md` 展开为实际文件和一跳依赖。

### 6.2 类型化阶段

```text
AcceptedMessage
→ AgentDecision
→ ToolInvocation
→ Observation
→ EvidenceDecision
→ CandidateResponse
→ PublicationDecision | ActionCandidate
→ ApprovalDecision
→ RuntimeEffectResult
```

- `graph.py` 只注册 Node / Edge 和顶层依赖，不保留私有方法转发壳；
- `policy.py` 不访问 Provider，不执行 SQL / MCP / Mutation；
- `tool_loop.py` 不决定业务授权；
- `actions/service.py` 不把 Memory 当作当前执行事实；
- API Projection 不改变领域状态；
- 测试绑定公开合同，不绑定 Graph 或 Service 私有方法。

### 6.3 复杂度预算与统计口径

| 项目 | 硬合同 / 建议值 |
| --- | --- |
| 默认入口 | 硬合同 `12` 个 |
| 一跳必读依赖 | 每入口硬合同 `≤2` |
| 单 Demo 必读路径 | 必须与冻结 Owner Map 一致，不得省略真实 owner |
| 必须掌握核心 | 目标 `6,000～8,000`、硬上限 `12,000` 非空非注释行；由 Code Map Manifest 列明文件 |
| 核心协调模块 | 建议 `<600` 行；超出必须书面说明 owner 与拆分理由 |
| 核心决策函数 | 硬合同 `<200` 行 |
| 前端 Hook / Reducer | 建议 `<300` 行；超出必须说明 |
| Runtime 依赖环 | 硬合同 `0` 个 |
| 重复业务 owner | 硬合同 `0` 个 |
| 当前权威文档 | 硬合同 `8` 份 |

行数统计覆盖 Code Map Manifest 标记的手写 `.py/.ts/.tsx/.sql`，包括
`db/security_contract.py` 及其手写 RLS / Grant / Capability 定义；排除空行、注释、由该合同
生成的 Baseline Migration、测试和 Archive。全 Runtime 不设总 LOC KPI，避免为了数字删除安全
校验。

当前权威文档固定为：`README.md`、`AGENTS.md`、本文、`architecture.md`、
`interview-guide.md`、`demo-runbook.md`、`operations.md`、`release-verification.md`。其余材料由
`README.md` 内的 History Index 指向 Archive，不另增第九份当前文档。

## 7. 当前正确性问题先闭环

结构搬迁前必须修复并增加回归：

1. Current Fact Claim Validation 识别否定、反义和同句纠正；
2. Customer 访问 `/approvals` 时重定向或明确拒绝，身份标签不得冒充 Approver；
3. Approval Source Window 包含 `origin_turn_id` 及其有界上下文，并支持继续分页；
4. Greeting-only 标题在第一条有效支持问题出现后持久更新；
5. Conversation / Approval 列表区分 Loading、Error、Empty，`closed` 不显示为连接中；
6. Billing ID 提取排除 ASCII 句末 `.` / `:`；
7. API Key 和 Entitlement 动作支持 Clause-level 否定后纠正；
8. `query_subscription` 的 Runtime 与 Memory Freshness 使用同一合同；
9. Hosted CI 零步骤失败如实记录为 GitHub Billing / Spending Limit 外部阻塞；
10. 当前 HEAD 不继承旧 SHA 的 `37/37`，验证报告分别列出历史、当前和未执行证据。

## 8. Archive、Validation 和 Baseline Schema 协议

### 8.1 Archive

- Archive annotated tag 必须直接指向基线
  `6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb`，不得指向后续 docs-only Authority Commit；
- 推送后验证 `git rev-parse <tag>^{commit}` 与基线 SHA 完全一致，同时记录 tag object SHA 与
  commit SHA；
- 生成逐文件 SHA-256 Manifest 和一份恢复说明；
- 在临时 worktree 完成一次 Restore Dry-run，验证历史文件与 Manifest；
- 不重写失败分数、Receipt、Matrix、Prompt、语料或受保护 Evaluation；
- 未满足这些条件前，默认分支不得删除历史文件。

### 8.2 Validation Tooling

- Phase 2 只拆 Runtime / Validation 包边界，不迁出或禁用历史测试；
- 先生成 Runtime Import Graph，把运行时仍依赖的 process / MCP contract 移入中立模块；
- 从实际 wheel 在空环境安装，执行 `python -I` import smoke，并启动 API、Worker、两个 MCP；
- Runtime wheel / 生产镜像排除 Acceptance、Eval、Evidence Builder、Diagnostics CLI；
- 历史测试必须与新合同测试并行到 Phase 6，不能先拆安全网再改核心。

### 8.3 Test Disposition Manifest

每组旧测试必须记录：

```text
old_test_group
→ requirement_ids
→ keep | replaced_by_new_contract | archive_only
→ replacement_test_nodes
→ before_result / after_result
```

Tenant、RLS、MCP 隔离、HITL、Checkpoint、Idempotency、Fence、Effect-once、Citation、PII 和
Provider fail-closed 必须有逐项前后映射。新旧测试并行通过后才能迁出旧 Oracle。

### 8.4 Baseline Schema

- 使用独立 Alembic 版本身份，只接受空数据库；检测到旧历史必须 fail closed；
- Seed 数据有独立版本 Hash，包含三条 Demo 的租户、客户、账单、Key、订阅、政策和事故资源；
- 生成旧最终 Schema 与新 Baseline 的表、字段、外键、Check、Unique、Index、Trigger、Function、
  RLS Policy 和 Grant 对比报告；
- 验证普通用户、API、Worker、MCP 身份的正负权限；
- 验证 one-active-approval、one-leased-job、Outbox、Idempotency 和 Effect-once 约束；
- 全新数据库 Upgrade、重复 Seed / Reset 和旧数据库拒绝启动必须自动化；
- Downgrade 和旧 Schema 原地升级明确不支持；Archive 只保证 Git / Schema Artifact 可恢复，
  不承诺迁移已有业务数据。

## 9. 测试、RAG 与 Provider 验证

### 9.1 可重复确定性验证

Unit、Contract、Integration、PostgreSQL/RLS、MCP、双 Worker、Frontend、Security、Wheel、Clean
Compose 和文档 Smoke 可重复运行。必须包含 Ruff / Lint、Mypy、Schema Hash、Provider
fail-closed、PII egress、Fence / Lease takeover、MCP reconnect / close 和零孤儿进程。

### 9.2 公开 RAG Dev Regression

冻结 30 条公开 Dev 样本，不访问 Evaluation v6 Holdout。保存原始分子、分母和排除项：

- eligible `Recall@5 ≥ 0.85`；
- eligible `MRR@10 ≥ 0.70`；
- Citation Binding Validity `= 1.00`；
- Unsupported Material Claim Rate `= 0`；
- 版本冲突与不可答样本安全拒答正确率 `≥ 0.90`；
- 在线 Reranker 保持关闭，除非另有用户批准和真实 A/B 证据。

### 9.3 真实 Provider Regression

Phase 0 冻结 `IE-P01...IE-P16`：8 类真实 Production / Native 语义，每类至少 2 个自然措辞，
其中至少 4 个为多轮；覆盖 429、退款、已退款、Key、Entitlement、跨租户、证据不足，以及
问候 / 身份 / 范围外后的自然续问。故障注入不计入这 16 条。

- 冻结 Candidate SHA、模型、Prompt Hash、Tool Schema Hash、语料 / Index Hash；
- 安全不变量必须 `16/16`：零越权 Effect、零无审批 Effect、引用不得伪造；
- 产品语义必须 `16/16`，失败如实保留；
- Runtime 内部有界 Transport Retry 仍可使用并计数；
- 同一 Candidate 的完整 16 条矩阵只执行一次，不得只重跑失败单题；
- 语义失败只能在通用修复形成新 SHA 后运行新矩阵，不按单题写专用分支；
- 该矩阵是公开回归，不是独立 Holdout，不声称未知问题泛化能力；
- 启动前保存预计调用数和费用；预计超过 CNY 30 时暂停确认。

任一完整 IE-P16 失败后，必须保存不可变 Receipt、调用费用、失败分类和清理证明，并立即进入
Confirmation Gate。未经用户批准，不得创建 replacement Candidate 或再次运行完整 IE-P16；
用户若批准 replacement，只授权一个新 Candidate，再次失败则再次停 Gate。

后续执行授权：用户于 2026-08-13 在两份失败 Receipt 均保存后，明确授予后续 clean Candidate 与
必要真实 DeepSeek 验证的持续授权，不再要求逐次确认。该授权只覆盖通用诊断、修复、完整前置证明
及每个新 Candidate 的一次完整 IE-P16；已消费 SHA、失败单题、Holdout、Cross-Encoder、历史 Gate /
Parity 与预计超过 CNY 30 的调用仍受原边界约束。

### 9.4 确定性故障矩阵

冻结独立的 `IE-F01...IE-F06`，使用显式 `fault-injected` 标记，分母与 IE-P16 分离：Provider
timeout、Provider auth failure、Malformed Envelope、MCP timeout / reconnect、Worker Lease /
Fence takeover、事务提交前后崩溃。它只证明 Runtime fail-closed、恢复和记账，不形成 Provider
质量结论，也不得混入 `production_native` 统计。

### 9.5 公开 Journey Manifest

Phase 0 冻结 `IE-J01...IE-J12`，不得在 Provider 结果出现后补分母：

- J01～J03：三条主 Demo；
- J04：已退款不重复申请；
- J05：API Key approve / effect-once；
- J06：Entitlement approve / effect-once；
- J07：reject 后继续咨询；
- J08：edit-and-approve 与事务重校验；
- J09：归档 / 恢复 / 继续对话；
- J10：MCP / Provider typed failure 与安全收敛；
- J11：双 Worker、重复恢复和重复点击；
- J12：角色切换、长会话审批来源和 Loading / Error / Empty。

每条固定身份、输入、Seed、允许工具、必需 Observation、Citation / HITL、终态、安全断言和页面
可见验收点。不得加入 Case ID 运行时分支。

## 10. 分阶段执行路线

### Phase 0：Authority、可逆性与冻结输入

- 完成 docs-only Authority Commit；
- 创建并验证 Archive annotated tag、Hash Manifest 和 Restore Dry-run；
- 冻结 12 入口、Owner / Dependency Matrix、IE-P16、IE-F06、IE-J12、RAG Dev30；
- 建立行为 Characterization 和 Safety Invariant Manifest；
- 未完成回滚证明前不得删除文件。

### Phase 1：正确性闭环

- 完成第 7 节十项问题；
- 运行定向和全量本地确定性验证；
- 触发 Hosted CI；若因 Billing、Spending Limit、Actions 权限或 Runner 配额零步骤，保存 Run
  URL 并登记为 Phase 7 Release Blocker；Phase 1 的本地正确性合同仍可完成并进入 Phase 2；
  本地 CI 不替代最终 Hosted CI；
- 不在本阶段进行大规模目录重排。

完成记录：Candidate `72ea297e466d77b68a75f007f12bc0cdeabca41b` 的本地正确性验证已完成；Hosted CI Run `31512749202` 因账户 Payment / Spending Limit 在 5 个 Job 中均为零步骤，已登记为 `external_zero_step_blocker`。这允许进入 Phase 2，但不构成 Hosted CI 通过，也不完成 Phase 7 或最终 Definition of Done。

### Phase 2：Runtime / Validation 包边界

- 生成 Import Graph，迁移运行时共享 Contract；
- 拆分可安装 Runtime 与 Validation Tooling；
- 运行 wheel / clean-environment / MCP / Compose Smoke；
- 历史测试仍全部可运行，不做 Pruning。

完成记录：Candidate `774c0f3490ece7a9a12ea2cbdf336a1328f2ff6c` 已通过 Runtime / Validation 双 wheel、clean-environment、完整 Runtime Import Graph、最终 Runtime-only 镜像、7-Service / 2-Worker Compose 与 Live MCP 验证；Runtime / Validation wheel RECORD 为 `188 / 81` 且零重叠，清理残留为 `0`。GHCR / PyPI TLS timeout 后仅以 SHA-256 `04792cac761c4a6ba78267f36f2af541b7f92196d42ac55d21d3ff6b0f5ab6a5` 固定的 Astral uv `0.11.2` GitHub Release 资产替代 `UV_IMAGE` 传输；默认 pinned GHCR 传输未直接验证，但 Dockerfile 其余逻辑与最终镜像已验证。Hosted CI Run `31520751057` 的 5 个冻结 Job 仍为零步骤外部阻塞。Phase 2 完成允许进入 Phase 3，不完成 Phase 7 或最终 DoD。

### Phase 3：Schema 与应用边界

- 建立 Baseline Migration、Seed Version 和 Schema Equivalence Manifest；
- 模型按 Auth、Conversation、Agent、Evidence、Action、Audit 拆分；
- 消除 `commands → runtime_jobs → approval_lifecycle → commands` 依赖环；
- 旧 Migration 继续保留到 Phase 6。

完成记录：Candidate `95a47f1932ea763045bd28dec8fe7877e9f2f147`（Tree
`61975bec3da27c4b3223a32f93e1ca9e2f0fbfd5`）已完成独立 Baseline、Seed / Reset、Schema
Equivalence、模型六域拆分与 Runtime SCC 收敛。历史 b207 与 `i200_baseline_0001` 的 18 个 Catalog
Section、3377 条记录全部等价，允许与阻断 Drift 均为 `0`；旧数据库在 DDL 前拒绝且保持不变。
Current Integration `298/298`、MCP Hermetic `6/6` + PostgreSQL `10/10`、Phase 3 确定性合同
`54` 条、Ruff 与 Mypy 均通过，清理残留为 `0`。Hosted CI Run `31538133178` 的 5 个冻结 Job
仍因账户 Payment / Spending Limit 为零步骤外部阻塞。Phase 3 完成允许进入 Phase 4，但不完成
Phase 7、最终 DoD、Human Acceptance、RAG Dev30、IE-P16、IE-J12、Holdout 或 Cross-Encoder。

### Phase 4：Agent、MCP 与 Action 收敛

- 移除 Graph 转发壳和不可达 escalation；
- 将 Decision、Tool Loop、Evidence、Policy 和 Action Pipeline 拆成类型化阶段；
- 三类动作复用一个端到端框架；
- 保持预算、安全失败和客户终态语义。

完成记录：Candidate `05f15760a97d05f55fa597456a2981df7d62f447`（Tree
`8b1c8d968d6192c30ec45e42a5b4dad11c673a7f`）已移除 Graph 转发壳与 live escalation，将
Decision、Tool Loop、Evidence、Policy 和三 Action Pipeline 收敛为类型化单 owner；12 个默认入口
共 `6839` 非空非注释行，核心决策函数不超过 `199` 行。Runtime Import Graph 为 `206 modules /
2754 edges / 0 SCC`，能力分母为 9 Read / 3 Proposal / 3 Runtime；i201 以两条精确 Catalog Delta
撤销 escalation，直调返回 `42501` 且零写。Current Integration `299/299`、MCP Hermetic `6/6` +
PostgreSQL `11/11`、Hermetic Backend `2204/2204`、Frontend `76/76` 与清理残留 `0` 均通过。
Hosted CI Run `31560564403` 的 5 个冻结 Job 仍因账户 Payment / Spending Limit 为零步骤外部阻塞。
Phase 4 完成允许进入 Phase 5，但不完成 Phase 7、最终 DoD、Human Acceptance、RAG Dev30、IE-P16、
IE-J12、Holdout 或 Cross-Encoder。

### Phase 5：前端状态收敛

- 拆分 Query、Stream、Mutation 和 View State；
- 增加角色路由守卫和真实身份表达；
- 修正长会话、审批来源、Loading/Error/Empty、归档连接状态和键盘交互；
- 保持 Conversation-first 视觉语言，不重新设计整套 UI。

完成记录：Candidate `70717d8f19a9cbe3d8ead99db228c93f1577acc4`（Tree
`853acfafd9782e2ce2d984cdd75da959718045a8`）已将 Conversation 与 Approval 的 Query、Stream、
Mutation 和 View State ownership 分离，页面不再直接持有资源状态或调用 API；真实身份路由、长会话、
审批来源、Loading / Error / Empty、归档连接、键盘焦点、移动端 390px 和远端审批结果自动对账均已验证。
Frontend `81/81`、浏览器验收 `19/19`、Hermetic Backend `2248/2248`、Runtime-only 镜像与 Clean
Compose 通过，具名资源清理残留为 `0`。Hosted CI Run `31565826533` 的 5 个冻结 Job 仍因账户
Payment / Spending Limit 为零步骤外部阻塞。Phase 5 完成允许进入 Phase 6，但不完成 Phase 7、最终
DoD、Human Acceptance、RAG Dev30、IE-P16、IE-J12、Holdout 或 Cross-Encoder。

### Phase 6：受控 Pruning

- 新旧测试并行全绿后，按 Test Disposition Manifest 迁出历史 Oracle；
- 按 Authority Transition 和 Archive Manifest 迁出历史文档、旧 Migration 和报告；
- 确认 Runtime wheel、默认工作区、8 份当前文档和 12 入口达到合同；
- 不删除 Archive 或历史 Git 可达性。

完成记录：Candidate `30254587585fa2169cab071a926c501e06dac9a6`（Tree
`199ca61783c5857cc95f83a468f1b80a5a313d81`）已按 SHA-256 Manifest 从当前工作区迁出 `2,197`
个历史文件、保留 8 份当前权威文档，并以 Archive Tag 与 Source Commit 验证可恢复性；Test
Disposition 的历史 Oracle 结果未改写。Hermetic Backend `1315/1315`、Current Integration
`225/225`、MCP `6/6 + 10/10`、Frontend `81/81`、双 wheel clean-environment 与 Runtime-only 镜像
均通过，具名资源清理残留为 `0`。Hosted CI Run `31573174199` 的 5 个冻结 Job 仍因账户 Payment /
Spending Limit 为零步骤外部阻塞。Phase 6 已完成并进入 Phase 7；本段只记录 Phase 6 关闭时点，
后续 Phase 7 的实际执行事实以紧随其后的执行记录和 Release Verification 为准。

Phase 7 执行记录：Candidate `b132c395c2edf2d7d72477dc9051bffc3d7f4024` 已通过 RAG Dev30、
IE-F06 `6/6`、IE-J12 `12/12`、确定性 / Integration / MCP / Browser / Clean Compose 证明及 Hosted
CI Run `31633888433`。该 SHA 唯一一次完整 IE-P16 执行 `16/16` 场景、通过 `11`、失败 `5`；
Safety 与 Cleanup 通过、Semantic 未通过，最高实际估算费用 `¥0.349337`。完整失败 Receipt 的
SHA-256 为 `68cf3f1d4c9bb8ade2fdca5b7b5d404cef3dc5822d751e34fbc416d245ec6bfa`。按本规范已停
Confirmation Gate；用户只批准一个新 Candidate 的通用修复，旧 SHA 不得重跑。Phase 7、Human
Acceptance 与最终 DoD 继续保持未完成。

Phase 7 replacement 执行记录：唯一获批 Candidate
`7527c0acca079f57549538e49135a91ef87b9389`（Tree
`b9d96a0dd984cf8874a00f8f00172ac6f34db4be`）通过 RAG Dev30、IE-F06 `6/6`、IE-J12 `12/12`、
Backend `1576`、Current Integration `225`、MCP `6 + 11`、Frontend `81`、Browser `19`、Clean
Compose、双 wheel、Runtime-only 镜像以及 Hosted CI Run `31664415941`。该 SHA 唯一一次完整
IE-P16 执行 `16/16` 场景、通过 `13`、失败 `3`；IE-P14/P15/P16 均记录
`scenario_execution_failed:ReadTimeout`。异常兜底在没有数据库用量快照时记录了 `0` token，因此
三项实际 Provider 用量和超时所在 HTTP 阶段均未知；已观测总量为 `248121 / 14710` token，
对应 Receipt 估算费用为 `¥0.277541`，实际总用量和费用可能更高。正式 Safety 与 Semantic Claim
均未通过，真实 external effect 为 `0`，
全部具名资源清理为零。replacement SHA 已消费且不得重跑；用户随后持续授权后续 clean Candidate
的必要真实 DeepSeek 验证。Phase 7、Human Acceptance 与最终 DoD 继续保持未完成。

### Phase 7：验证与作者移交

- 运行确定性、RAG Dev30、IE-F06、IE-P16、IE-J12、Hosted CI 和 Clean Compose；
- 生成 `release-verification.md`、三条 Trace Walkthrough 和 30 个高频问答；
- 实现 Agent 停在 Review Gate，由用户本人完成作者掌握验收；
- 用户验收后才把 Interview Edition 设为默认展示版本。

每个 Phase 结束必须逐项回查本 Phase 合同。未满足条目继续留在原 Phase 修复，不得用“已提交”
代替“已完成”，也不得进入下一 Phase 隐藏遗留问题。唯一例外是 Phase 1 明确登记的 Hosted CI
外部 Release Blocker；它不阻止 Phase 2～6，但 Phase 7 与最终 DoD 必须保持未完成。

## 11. 作者掌握计划

实现 Agent 只负责生成材料和自测，不得代替用户勾选作者掌握 DoD。

| Session | 预计时间 | 必读入口 | 必须产出 / 回答 |
| --- | --- | --- | --- |
| 1. 产品与架构 | 2 小时 | README、Architecture、`graph.py` | 手画请求总图；解释单 Agent、PostgreSQL、Redis |
| 2. Read Tool Loop | 4 小时 | `decision.py`、`tool_loop.py`、`mcp/runtime.py` | 画 Decision / Observation 时序；跟读成功与失败 Trace |
| 3. RAG 与回答可信度 | 4 小时 | `rag/service.py`、`evidence.py` | 手算一次 RRF；解释 Citation、冲突、拒答和指标 |
| 4. HITL 与 Effect | 4 小时 | `policy.py`、`actions/service.py` | 画 Approval / Resume / Effect；解释幂等和重校验 |
| 5. 多租户与失败恢复 | 4 小时 | `messages.py`、`worker.py`、`db/security_contract.py` | 读一次 RLS / Grant；解释 Outbox、Lease、Fence、fail closed |
| 6. 产品界面 | 2 小时 | `App.tsx` 与两个 Page | 演示客户 / 审批者状态；解释 Loading/Error/Empty |
| 7. 模拟拷打 | 2 × 2 小时 | 30 问、三条 Trace | 随机抽 10 问，8/10 正确；任抽一条 Demo 可定位到源码 |

最终 Human Acceptance 要求用户：不看答案完成 15 分钟主讲；随机抽取 10 个问题至少正确回答 8
个；从三条 Demo 中随机选择一条，在 5 分钟内定位入口、关键类型、数据库终态和失败路径。

## 12. Definition of Done

### 12.1 产品与安全

- [ ] IE-J12、IE-P16、IE-F06 与三条 Web Demo 满足各自冻结合同和独立分母；
- [ ] Agent 有真实 Observation 回流和 Replan；
- [ ] RAG Dev30 达到第 9.2 节指标；
- [ ] 三类 Action 复用一套完整 Proposal / Approval / RuntimeEffect；
- [ ] Tenant/RLS、Policy、HITL、Checkpoint、Idempotency、Fence 和 Effect-once 不回退；
- [ ] 第 7 节十项问题全部关闭。

### 12.2 结构与可读性

- [ ] 总 Code Map 恰为 12 个入口，每入口一跳必读依赖 `≤2`，三条 Demo 与冻结 Owner Map 一致；
- [ ] 必须掌握核心目标 `6,000～8,000`、绝对上限 `12,000` 非空非注释行并有 Manifest；
- [ ] 核心决策函数全部 `<200` 行，Runtime 依赖环和重复业务 owner 均为 `0`；
- [ ] Graph 无转发空壳或不可达 escalation；
- [ ] 前端 Query / Stream / Mutation / View State 边界明确；
- [ ] Runtime wheel / 镜像不含 Validation CLI，空环境安装和启动通过；
- [ ] 当前权威文档恰为 8 份；Archive Hash、远端可达和 Restore Dry-run 通过；
- [ ] Baseline Schema、Seed、权限与约束等价报告通过，旧数据库 fail closed。

### 12.3 验证与诚实边界

- [ ] Unit、Contract、Integration、PostgreSQL/RLS、MCP、双 Worker、Frontend、Ruff/Lint、Mypy、
  Security、Wheel、Clean Compose 和 Docs Smoke 全绿；
- [ ] MCP Discovery、Schema Hash、Allowlist、重连、关闭、权限隔离和零孤儿进程通过；
- [ ] Provider fail-closed、PII egress、Lease/Fence takeover 和 Effect-once 通过；
- [ ] Hosted CI 实际启动并完成，不是零步骤；若账户阻塞则保持未完成；
- [ ] Docker Builder、镜像、Volume、临时目录和 MCP 子进程清理可证明；
- [ ] `release-verification.md` 区分历史 `37/37`、当前结果和未执行 Evaluation；
- [ ] Evaluation v6、Cross-Encoder、真实外部 Effect 和生产 SLA 未被夸大。

### 12.4 作者所有权（最终 Human Acceptance）

- [ ] 用户本人完成 15 分钟主讲；
- [ ] 随机 10 问至少答对 8 问；
- [ ] 随机 Demo 能在 5 分钟内定位入口、关键类型、终态和失败路径；
- [ ] 用户能解释 Agent、RAG、MCP、HITL、Memory、Queue、多租户和三个未实现边界。

## 13. Confirmation Gates

- 将本文改为“已批准并冻结”并创建 docs-only Authority Commit；
- 下达 Phase 0～7 执行 Prompt；
- 创建 / 删除 Archive Tag、Branch，迁出默认分支文件或替换 Migration；
- 新建、重命名、删除 GitHub 仓库或修改可见性；
- 改变三条 Demo、三类 Action 或核心安全规则；
- Hosted CI 因 Billing、Spending Limit、Actions 权限或 Runner 配额未启动；
- 真实 Provider 预计成本超过 CNY 30；
- 任一完整 IE-P16 失败时消费该 Candidate 并保存证据；后续 clean Candidate 已有持续授权，但不得
  重跑同一 SHA 或选择性重跑失败场景；
- 访问 Evaluation v6 Holdout、运行 Cross-Encoder 或历史 Gate / Parity；
- 将 Interview Edition 设置为默认展示版本；
- 全部工程 DoD 完成，等待用户本人做作者掌握验收。

普通代码提取、测试失败、依赖冲突、UI 细节和文档修正，在明确执行 Prompt 授权后的冻结范围内
由执行者处理，但必须遵守 Phase 回查和 Test Disposition，不得扩大范围。

## 14. 审批建议

建议批准本文档方向。批准后的第一步只创建 docs-only Authority Commit，不开始代码改动。随后由
用户单独下达执行 Prompt，授权 Phase 0～7。实施顺序固定为：先保证可逆，再修正确性，再拆包和
结构，最后才 Prune 历史安全网；不得再次采用“发现一个问题就新增一个 Addendum”的滚动方式。
