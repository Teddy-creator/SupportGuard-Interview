# Operations

当前实施权威是 `docs/interview-edition-simplification-v2.0.md`，操作边界由根目录 `AGENTS.md`
规定。v1.2～v1.7 的 Gate、Receipt 和 Verification 只作为历史证据，不能重新消费；当前 HEAD
也不继承历史 Candidate 的通过结论。

## 当前 Phase 证据边界

Phase 1～6 均已完成。Phase 7 历史 Candidate
`b132c395c2edf2d7d72477dc9051bffc3d7f4024`、`7527c0acca079f57549538e49135a91ef87b9389`
的一次性 IE-P16 为 `11/16`、`13/16`，失败 Receipt 位于
`validation/evidence/interview_v2/phase7/attempts/` 且不可重跑。用户持续授权 clean Candidate 的
必要真实 DeepSeek 验证；最终工程 Candidate
`4466290963993e0b7662d75b571e4b15e4e97627` 已通过全套确定性 / 集成 / MCP / Browser / Clean
Compose、RAG Dev30、IE-F06、IE-J12、Hosted CI Run `31687980408` 与一次性 IE-P16 `16/16`。
Phase 7 机器验证和工程 DoD 已完成，最终 Definition of Done 只等待用户 Human Acceptance。

## Interview Edition Archive 恢复

精简前完整仓库由 annotated Tag `archive/interview-v2.0-baseline` 固定；该 Tag 必须解引用为
`6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb`。逐文件 SHA-256 Manifest 与恢复 Receipt 位于
`validation/evidence/interview_v2/phase0/`。恢复演练只创建临时 detached worktree，不运行历史
Gate、Provider 或受保护 Evaluation：

```bash
restore_dir=/tmp/supportguard-v2-restore-dry-run
git -c core.hooksPath=/dev/null worktree add --detach \
  "$restore_dir" archive/interview-v2.0-baseline
uv run python scripts/archive_interview_v2.py verify \
  --manifest validation/evidence/interview_v2/phase0/archive-manifest.v1.json \
  --checkout "$restore_dir" \
  --receipt /tmp/supportguard-v2-restore-receipt.json
git worktree remove "$restore_dir"
```

验证结束只能删除本次命名的 worktree，并确认临时目录和 worktree registry 均无残留；禁止全局
清理或删除其他 worktree。Git 中只保存 tracked blob 与外部证据的既有 Hash 引用，不宣称已归档
仓库外 raw Provider payload。

## 组件

API 只接收 Command、读取 Query Model 与提供 SSE；Dispatcher 发布 Outbox；Reconciler 修复未发布、到期 Lease 与 Retry Job；每个 Worker 并发度 1，并拥有两个长期 MCP Session。

## 健康与内部详情

```bash
curl /api/health/live
curl /api/health/ready
curl -H "X-Internal-Token: $INTERNAL_API_TOKEN" /internal/health/dependencies
```

Public Ready 只返回 `{"status":"ready"}`、`{"status":"read_only"}` 或
`{"status":"unavailable"}`。依赖名、Backlog、Worker、RuntimeTiming 和 Redis 状态只出现在
正确 Internal Token 下的内部接口；缺失或错误 Token 均返回同形 404。Redis 故障不让已接受
Job 丢失，新命令是否接收由持久 Admission Snapshot、Backlog 与 Upgrade Fence 决定。
Worker、Dispatcher 与 Reconciler 每 10 秒写与当前 RuntimeTiming 版本和配置 Hash 绑定的
ServiceHeartbeat。

## 数据库与迁移

只有 one-shot migrate 执行 Alembic。Bootstrap Demo 幂等写 Fixture、MCP Role 与 Knowledge Index。RLS 测试使用非 Owner Role，未设置 Tenant 时返回 0 行。

## 开发构建与启动

```bash
make dev          # 镜像存在时直接 up --no-build
make dev-build    # 只显式构建，不启动
make dev-rebuild  # 显式重建后启动
```

共享 Backend Image 只有一个 Compose Build Owner；依赖、固定 E5 revision 与源码分层缓存，源码或文档变化不会重新下载模型。上述入口适合交互式开发，不构成正式候选的 Build/Cache 所有权证明。它们不执行 Docker prune、Volume 删除或历史 Cache 清理。

## 具名 Demo 生命周期

稳定入口只管理显式 `supportguard*` Compose Project：

```bash
make demo-inventory
make demo-start DEMO_PROJECT=supportguard-v15ui
make demo-stop DEMO_PROJECT=supportguard-v15ui
make demo-reset DEMO_PROJECT=supportguard-v15ui CONFIRM_PROJECT=supportguard-v15ui
make demo-preflight DEMO_PROJECT=supportguard-v15ui
```

正式候选或干净验收从 clean Git worktree 使用具名 Buildx Builder，并把完整 Commit 同时写入
OCI revision 与 Runtime Manifest：

```bash
DEMO_FAKE_PROVIDER=true make demo-reset \
  DEMO_PROJECT=supportguard-v155-phase4 \
  CONFIRM_PROJECT=supportguard-v155-phase4 \
  BUILD=1
```

该命令派生且只拥有：

- Builder：`supportguard-v155-phase4-builder`；
- 镜像：`supportguard-backend:supportguard-v155-phase4` 与
  `supportguard-frontend:supportguard-v155-phase4`；
- Compose Project/Network/Volume：`supportguard-v155-phase4` 命名空间。

`reset` 删除并重建该具名 Project 的容器、网络和 Volume，适用于可丢弃的专用 Interview Demo；不得对保存审计历史或所有权未知的 Project 执行。仅停止时不会删除 Volume。需要 teardown 并删除专用卷时必须同时提供：

```bash
make demo-teardown \
  DEMO_PROJECT=supportguard-v15ui \
  DELETE_VOLUMES=1 \
  CONFIRM_PROJECT=supportguard-v15ui
```

验收结束先删除同名 Project/Volume，再执行安全 `cleanup-build`。它会先拒绝仍被容器引用的
镜像，然后只删除上述两个派生镜像与同名 Builder/Cache：

```bash
make demo-teardown \
  DEMO_PROJECT=supportguard-v155-phase4 \
  DELETE_VOLUMES=1 \
  CONFIRM_PROJECT=supportguard-v155-phase4
make cleanup-build \
  DEMO_PROJECT=supportguard-v155-phase4 \
  CONFIRM_PROJECT=supportguard-v155-phase4
```

空名、`default`、模糊 Project、未知 Volume 和全局 `docker system prune -a --volumes` 都不属于受支持流程。`docker system df` 是当前 Docker 对象统计；Docker Desktop 稀疏磁盘的逻辑文件大小不等于可安全回收字节。

## Bounded Formal 与 v1.5.12 Journey Acceptance

历史 v1.5.10 Bounded Formal 的原始结果永久为 `11/12`；v1.5.11 只对同一不可变 Receipt
离线裁决为 `12/12`，`provider_rerun=false`。这不是当前 v1.5.12 的完整旅程验收。

v1.5.12 已冻结 `23` 条 Journey、`37` 个原子子场景，其中 `19` 个包含
Production/Native Provider 语义，`18` 个为纯确定性场景。当前 Matrix 与 Evidence
Manifest 都是 `candidate_sha=null`，Manifest 为 `execution_state=unexecuted`：

```bash
PYTHONPATH=backend/src:validation/src:scripts \
  uv run python scripts/run_real_user_journeys_v1512.py plan
```

- Matrix SHA-256：
  `f07f2ba30f4a84d871f6b645b278d9b403470dfd67b0fd76b56dc3923dbc8e34`；
- Evidence Manifest SHA-256：
  `275f13a82433e9a3d076f1d9f78fecc9c6fc13a3896dc66fc706794a0fabf3cd`；
- exact registry 模块：
  `supportguard.acceptance.journey_executors_v1512`。

正式执行必须先由同一 clean pushed SHA 的 Backend、PostgreSQL/RLS、MCP、Two-worker、
Frontend、Security、Clean Compose 和 Docs 八条 Lane 生成真实命令 Artifact 与
Attestation。仓库唯一合同位于
`supportguard.acceptance.journey_preflight_contract_v1512`；不接受运行者提供
Allowlist、替代 Lane Plan 或手写 pass。

```bash
# 只查看固定计划，不启动服务或消费 Provider/Formal 授权
make v1512-journey-preflight-plan

# 仅在 clean HEAD == origin/main 上运行；两个输出位置必须在仓库外
make v1512-journey-preflight \
  V1512_INVOCATION_ID=v1512-preflight-<timestamp> \
  V1512_ATTESTATION=/absolute/external/v1512-preflight.json \
  V1512_EVIDENCE_ROOT=/absolute/external/v1512-preflight-evidence
```

Runner 为一次候选拥有完整 Compose 生命周期，隔离 PostgreSQL/Redis 测试载体，运行恰好
19 条无 API Mock、无 Agent 消息的候选栈 Playwright，并校验 `pip-audit`、候选镜像
ID/Revision、容器 `CODE_VERSION`、服务拓扑、Readiness 和 b207 Head。每条命令的结果由
JUnit、stdout/stderr 或 Runtime 查询重新计算；命令失败也必须留下 Receipt。启动、Lane
或最终校验失败都进入同一 `finally`，删除本轮 Container、Network、Volume、Image、
Buildx 与临时目录并证明 residual 为零。Preflight 固定校验 Matrix/Manifest Hash，但
不执行 37 项 Journey、不调用 DeepSeek、不修改 `execution_state=unexecuted`，也不消费
Formal 授权。首次正式真实场景开始后该候选调用才被消费；失败先保存有限 Snapshot，
通用修复进入新 SHA，不能原 SHA 重跑。

镜像构建默认使用本轮命名 Buildx Builder；如果 Docker Registry 的 TLS/Token 元数据
不可用，但三个固定 Base Image 已经在共享 Daemon 中完整存在，Runner 会显式切换为
`docker build --pull=false`，并在 `prepared_runtime.build_mode` 记录
`shared-daemon-local-base`。这只是构建传输降级，不跳过候选镜像 Revision、容器
`CODE_VERSION`、服务拓扑、Readiness 或清理验证；Base Image 不完整时仍失败关闭。

该消费由 Runner 在第一个场景和 Provider 调用前，以 Candidate SHA 在真实系统用户目录
`.local/state/supportguard/v1512-journey/` 原子创建 `0600`、`O_EXCL` 记录。更换
invocation、journal、receipt 或 evidence root 都不会获得第二次执行资格；失败或进程
中断也不会删除记录。

PostgreSQL、Redis、API、Frontend 与可选 Tempo 默认只发布到 `127.0.0.1`；容器之间使用内部
Compose Network。Development Demo Auth 不能作为 Production Auth 使用，`.env.example` 的直接
开发 Host 也保持 Loopback。所有服务的 `json-file` 日志上限为单文件 10 MB、最多 3 个文件。
最终 Receipt 分别记录 Images、Volumes、Build Cache 与稀疏磁盘逻辑大小，不能把它们相加成
“可回收空间”。

当前 Alembic Head 为 `b207c0a1d001`。b193 令 SQL canonical JSON 的 key 排序显式使用
`COLLATE "C"`；b194 以 forward-only 方式把 Accepted Turn 运行身份、Dead Job 收敛和
完成时间语义安装到既有数据库；b195 只在已提交动作绑定与实际效果完全一致时返回幂等重放；
b196 按 RetrievalTrace 的冻结候选序号解析审批证据 Chunk；b197 令普通 Approve 的空理由
与公开 HTTP 合同一致；b198 统一 Edit-and-Approve 的 Selected Revision、客户 Action Card、
终态消息与审批 Diff；b199 在零副作用收敛事务内追加可哈希追溯的 `runtime_failed`
终态事件；b200 撤销内部 Reconciler 实现函数被误授予的运行时直调权限；b201 以
失败关闭的回填和同一 Owner Trigger 保持 Customer Message、Turn、Run 绑定一致，但不放宽
Reject/Edit 或任何执行前置条件；b202 只允许字段严格匹配的无锚点 Historical 安全拒答
终态，并将上下文旧版本追问与 Compare 语义对齐；b203 为有界客户事件投影增加稳定
Durable Event ID；b204 将同一真实身份补齐到工单详情与技术检查器 Timeline，使所有公共
Event DTO 生产者一致，同时仍不暴露原始 Event Payload 或 Hash 链。
当前 b206 只收紧 Conversation Page 读路径：先按 Cursor/Limit 选择 Turn，再在同一次
Capability 中返回 Message、Run 与 Citation；它不改变 Writer Contract Generation 或动作语义。
它们不构成 Journey Acceptance 已执行或通过的证据。Worker 只有在 Canonical Runtime Manifest、
Provider/Limiter、两个 MCP Session/Schema、消费进度和 Migration Head 同时满足契约时才
发布 `ready`；Heartbeat 写入与 `__healthcheck__` 读取保持同一 JSONB Capability 类型。
内部依赖快照可查看有界组件事实，公开 `/health/ready` 仍只暴露就绪结论。审批列表是
Pending-first 的有界轻量 Projection；审批详情是严格安全 DTO，来源会话最多返回 `100`
条客户/助手消息并标出原始 Turn 与截断状态。干净 Demo Migration、Seed、E5 Ingest、
Temporal Refresh 与 Resource Preflight 都由 Compose one-shot 服务/
`make demo-preflight` 验证，不应在长期共享 Volume 上手工把已消费 Fixture 改回初态。

## 公开错误与客户失败

HTTP 边界只返回
`ProductProblem(public_code, message, retryable, request_id)`。任意 HTML、Stack Trace、
内部错误码、Provider 原始错误体或 MCP Raw Error 都必须在进入产品投影前归一化。客户运行
失败只允许分为 `api_request`、`provider`、`tool`、`runtime` 四类，并用五段式回答说明：
已检查内容、已确认事实、未知部分及原因、审批/执行状态、下一步。

当前产品没有 Operator Inbox、人工回复、Resolve 或 SLA。技术失败不能生成
`manual_takeover` 或承诺有人接单；历史 `human_queue/manual_takeover` 只读说明当前无人工
闭环。未进入合法历史 Human Queue 的失败 Ticket 仍可接受下一条客户消息并创建新的
串行 Agent Run。

## Retention

```bash
supportguard maintenance retention --dry-run
supportguard maintenance retention --apply
```

逐表、default-deny Retention Manifest 覆盖当前全部 ORM/Checkpoint 表；只有明确 allowlist 的低风险叶节点在终态、TTL 和引用条件满足时可删除。AgentEvent、Approval、HumanDecision、BusinessAction、Capability/Finalizer 证明等高风险事实不自动删除。运行报告记录每个表的 eligibility、删除数、阻塞原因和清单版本；最终跨租户与重复 apply 证明由 v1.2.6 detached Gate 生成。

## 故障语义

- XADD 后 DB 未标记：允许重复 Delivery，Inbox / Fence 吸收。
- DB Commit 后 XACK 前崩溃：允许重投，状态机与唯一动作身份吸收。
- Worker 在 lease 期间被杀：Reconciler 先把过期 PostgreSQL lease 恢复为 queued，再由 `XAUTOCLAIM` 复用原 PEL delivery；不能创建重复 generation，也不能让 claim 失败反复重置 idle。
- Provider / MCP 外发后 Worker 丢失：Attempt 保持 consumed / unknown，不能免费重试。
- 外部系统不支持幂等且结果不可查询：Approval 保持 Active 并投影
  `verification_pending`；Reconciler 权威核验前不盲目重试，也不宣称已转人工。

## 可选本地 Trace

```bash
make dev-build
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces \
  DEMO_FAKE_PROVIDER=true \
  docker compose --profile observability up -d --no-build --scale worker=2
curl 'http://localhost:3200/api/search?limit=20'
```

OTel 使用 W3C `traceparent` 经 PostgreSQL Outbox 和 Redis Message 传播。Tempo 只在
`observability` Profile 中启动；默认 Compose 不增加常驻资源。已验证同一 Command Trace
同时包含 API `HTTP POST` 与 Worker `runtime.job.consume` Span。JSON 日志在 Filter 与 Formatter
两层递归清理 message、args、extra、exception 与 nested payload，并覆盖 Secret、邮箱和手机号；
安全 `error_code` 保留。业务 ID 只进入 Log / Trace，不进入 Prometheus Label。

Runtime Stream 的普通 `XADD` 不自动 Trim。维护操作先使用 dry-run：

```bash
supportguard maintenance redis-trim --dry-run
supportguard maintenance redis-trim --apply
```

维护入口逐条核验 PostgreSQL Job 已为 `succeeded/dead`、所有 Consumer Group 的 PEL 无该 ID、
envelope 与持久 Job 一致，且 `runtime-thresholds.v1` 的 600 秒窗口已过；随后使用 `XDEL` 并写
`QueueDeliveryAudit`。活跃、缺失、过新、PEL 中或身份错配的 Delivery 均只计入 skipped。

## 本地负载验收

先以 Fake Provider、两个 Worker 和提高后的本地 Admission Fixture 启动：

```bash
DEMO_FAKE_PROVIDER=true make dev-build
TENANT_COMMANDS_PER_MINUTE=10000 PRINCIPAL_COMMANDS_PER_MINUTE=10000 \
FALLBACK_COMMANDS_PER_MINUTE=1000 DEMO_FAKE_PROVIDER=true \
docker compose up -d --no-build --scale worker=2
make load-test
```

Harness 会创建无语义随机 Billing / Ticket Fixture，执行 30 秒 Warmup 与 30 秒计量，并写入
`evals/reports/load-v1.2.json`。它不调用真实 Provider，不应与真实模型延迟或成本混算。

## 进程故障验收

先使用当前 Backend Image、Fake Provider、两个 Worker 启动。为稳定命中持 Lease 崩溃窗口，
仅在该 Fixture Run 显式设置 `DEMO_FAKE_PROVIDER_DELAY_SECONDS=2`，然后运行：

```bash
make test-faults
```

命令会先强制重建 `DEMO_FAKE_PROVIDER=true` Worker 并验证环境，再运行确定性 Queue / Segment / Attempt 回归，真实停止并恢复 Redis、SIGKILL 当前 Lease
Owner，验证 PostgreSQL Durable Fallback、Reconciler、递增 Fencing Token、120 秒上界和重复
BusinessAction 为 0。该命令会可逆地重启本地 Compose 组件，不应对共享环境运行。

## v1.2.4 Corrective Evidence（历史，后置审计未通过）

以下命令和 Artifact 保留用于复现当时提交，不再证明当前 `36/36 fixed`。后置审计确认 Requirement 映射、实际 Evidence Type/Role 与完整 Gate orchestration 存在缺口；不要在共享环境运行，也禁止在含五个 protected untracked 文件的主工作树运行。即使在 tracked-only 隔离工作树重跑，也不得描述为当前 v1.2.6 通过。

最终 Tested Code Commit 为 `70124bd2e6d5baa2b954bfd7804da152a75c84fe`。确定性证据环境必须显式使用 Fake Provider；`phase3_provenance.py` 会在 Worker 不是 `DEMO_FAKE_PROVIDER=true` 时先行失败，禁止误用真实 Provider 生成正确性证据。

```bash
DEMO_FAKE_PROVIDER=true docker compose up -d --build --scale worker=2
make corrective-v124-gate
uv run python scripts/process_faults.py
uv run python scripts/phase2_recovery.py
make corrective-v124-evidence \
  MANIFEST=evals/reports/evidence/v1.2.4-closure-70124bd.json
```

当时报告记录 100 个已接受任务全部成功、dead/lost/duplicate effect 为 0、Worker kill 后 fence 1→2、Redis 恢复后同 run 收敛且 PEL=0；这些数值是历史回归输入，不代表任何 v1.2.6 Requirement 已在同一 Tested Tree 上关闭。完整历史 Artifact Hash、限制和后置审计状态见 `docs/corrective-review-gate-v1.2.4.md`。

## v1.2.6 Corrective Gate

稳定入口、clean-worktree/unique-Compose、Evidence 路由与 71 项分母冻结在 `docs/production-corrective-hardening-v1.2.6.md`。专项 `corrective-v126-*` target 只提供调试证据，不产生最终 Closure；最终验收只允许从 frozen tested commit 调用一次 `make corrective-v126-gate`。Review Packet 在 evidence-only commit 中保持 `human_review_status=pending`，不得把机器 Closure 等同于用户审核通过。
