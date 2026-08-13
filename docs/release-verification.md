# SupportGuard Interview Edition Release Verification

> 当前权威：`docs/interview-edition-simplification-v2.0.md`。本文件只记录精确 Candidate 的实际
> 结果；历史结果、当前结果与尚未执行项必须分开，不能相互继承。

## 当前状态

- 当前阶段：Phase 7 Review Gate / Human Acceptance；Phase 0～7 机器验证与工程 Definition of Done
  已完成，最终 Definition of Done 尚未由用户本人验收；
- 当前已测试 Candidate：`4466290963993e0b7662d75b571e4b15e4e97627`，Tree
  `f4d021c13eac823d807cf3d120a99a610df9bb7b`，执行时 `HEAD == origin/main` 且 Worktree clean；
- 历史失败：`b132c395...` 与 `7527c0ac...` 已消费且不得重跑，一次性 IE-P16 分别为 `11/16`、
  `13/16`；两份失败 Receipt 原样保留。用户于 2026-08-13 持续授权后续 clean Candidate 的必要
  真实 DeepSeek 验证，最终 Candidate 已按该授权消费且完整通过；
- Archive：annotated Tag `archive/interview-v2.0-baseline` 已远端验证并恢复演练通过；
- Hosted CI：最终 Candidate Run `31687980408` 已完成，冻结 5 个 Job / 76 个 Step 全部成功；
- IE-P16：最终 Candidate 唯一一次真实 DeepSeek 执行 `16/16`，Safety、Semantic、Usage、Cleanup
  与完整矩阵均通过，估算费用 `¥0.308857`；
- Phase 7 机器验证与工程 Definition of Done：已完成；Human Acceptance 与最终 Definition of
  Done：未完成。

## 历史证据（只读，不可继承）

精简前 Candidate `e68715f576a971ca57f78858dc964dd86b39f96e` 的历史 Journey 结果为
`37/37`。该结果绑定当时的 b205、SHA、Schema、Prompt、Seed 与执行载体；它不证明当前 HEAD、
b206/b207、v2.0 结构或后续 Candidate 已通过。

## 当前已执行证据

Phase 0 已完成，证据入口为 `validation/evidence/interview_v2/phase0/`。

Phase 1 Candidate `72ea297e466d77b68a75f007f12bc0cdeabca41b` 的本地正确性验证已完成：

- Hermetic Backend：`2006 passed / 380 deselected / 18 warnings`，`282.64s`；
- 隔离 PostgreSQL / Redis：`320/320`，`source_dirty=false`，Source State SHA-256
  `e6a9cde8b366eb0d68c34d9079ed6c25d703df7e0975c8bb4a8d99e218d19931`，测试数据库已删除；
- MCP：`50/50`；
- Frontend：`5 files / 76 tests`，Lint 与 Build 通过；
- Docs、Ruff、Scoped Format、Mypy（250 个 Source）、Security（10 Services / 5 Secret Vars）通过；
- `pip-audit`：No known vulnerabilities；
- 临时 Phase 1 PostgreSQL / Redis 已移除，测试数据库与测试进程残留均为 `0`。

本地 Receipt：`validation/evidence/interview_v2/phase1/local-validation-receipt.v1.json`。

Phase 2 Candidate `774c0f3490ece7a9a12ea2cbdf336a1328f2ff6c` 的 Runtime / Validation 包边界已完成：

- Backend：`2086 passed`；Frontend：`76 tests`，Lint 与 Build 通过；
- Ruff 通过；Mypy 覆盖 Runtime `174` 与 Validation `77` 个 Source；Security Boundary
  `10 Services / 5 Secret Vars`，`pip-audit` 为 No known vulnerabilities；
- Runtime / Validation wheel RECORD 分别为 `188 / 81`，路径重叠为 `0`；Runtime Import Graph
  `174 modules / 2219 edges`，从 6 个 Runtime Root 到 Validation 的禁止可达为 `0`；
- clean Runtime 环境不暴露 Acceptance、Diagnostics、Eval、Evidence 或 Validation，Runtime CLI
  不暴露 `eval`；安装双 wheel 的 Validation 环境仍可运行历史 Tooling；
- 最终 Runtime-only 镜像 Inventory 与 Smoke 通过，镜像 Revision 精确绑定 Candidate；
- Clean Compose 验证为 `7` 个 Service、`8` 个 Container、`2` 个 Worker 与 `4` 个 MCP 子进程；
  Live Read / Action MCP Schema Hash 已记录，inert 调用拒绝，Runtime execute 未被发现；
- Compose、MCP 子进程、具名 Builder 与临时镜像清理残留均为 `0`。

Phase 2 Receipt：
`validation/evidence/interview_v2/phase2/phase2-validation-receipt.v1.json`，SHA-256
`1f8f35b14d608d339e2fc56ccfb954465fe6c3f848c3e84c62f2380bd0463b7b`。包边界原始 Candidate
载体位于 `dist/phase2/package-boundary-774c0f3490ece7a9a12ea2cbdf336a1328f2ff6c.json`。

镜像传输限制：默认 pinned
`ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36`
与 PyPI 路径发生 TLS timeout，因此默认 pinned GHCR transport **未直接验证**。本次验证只通过
`UV_IMAGE` Build Arg 使用 Astral uv `0.11.2` 官方 GitHub Release 的
`uv-aarch64-unknown-linux-gnu.tar.gz` 作为传输替代；资产 SHA-256 为
`04792cac761c4a6ba78267f36f2af541b7f92196d42ac55d21d3ff6b0f5ab6a5`。该替代不改变 Dockerfile
其余构建逻辑；最终 Runtime-only 镜像、Inventory、Smoke 与 Compose 均已实际验证，但不能据此
宣称默认 GHCR 传输已通过。

Phase 3 Candidate `95a47f1932ea763045bd28dec8fe7877e9f2f147`（Tree
`61975bec3da27c4b3223a32f93e1ca9e2f0fbfd5`）的 Schema 与应用边界已完成：

- 历史 b207 与独立 `i200_baseline_0001` 的 Catalog Equivalence 为 `18/18` Section、
  `3377/3377` 记录；允许 Difference `0`、阻断 Difference `0`；
- 旧 b207 数据库由当前 Baseline 在 DDL 前以 `interview_baseline_legacy_database_rejected`
  拒绝，Revision、Catalog Hash 与 3377 条记录保持不变；
- Seed `interview-seed.v1` 重复执行语义相等；Drop/Recreate Invocation-owned Database 的真实
  Reset 已执行且语义相等，普通 Seed 保留可变业务状态；
- Current Integration `298/298`，`source_dirty=false`，Source State SHA-256
  `47bdab4da5cda1a6939fe28d15686c87518230a5424e731c531bb5e358712e13`；33 个历史节点和 1 个
  Dedicated Schema 节点按 Manifest 排除，测试数据库已删除；
- MCP Hermetic `6/6` + PostgreSQL `10/10`，失败 `0`、跳过 `0`、孤儿进程 `0`；Manifest SHA-256
  `5a44a07633939904eeceb41977806b3cbdbdf3f02144fab35fdf4b4ed73a081a`；
- Phase 3 确定性合同 `54` 条通过；Ruff 通过；Mypy 覆盖 Runtime `190` 与 Validation `79` 个
  Source；
- Invocation-owned PostgreSQL / Redis、具名与匿名 Volume、Label-matched Container / Network /
  Volume、55433 / 56379 Listener、MCP / Runner 子进程残留均为 `0`。

Phase 3 durable Receipt：
`validation/evidence/interview_v2/phase3/phase3-validation-receipt.v1.json`，SHA-256
`cc4032659d78c1be447bc2e7d73afdcec71cd4ed91e20db84c9a1d391e2fad84`。Schema 原始载体位于
`dist/phase3/baseline-schema-95a47f1932ea763045bd28dec8fe7877e9f2f147.json`，SHA-256
`d57a8ff70375004ab39d90d8697f0dd415ed9de21614d2391d8d79990299f7e1`。

Phase 4 Candidate `05f15760a97d05f55fa597456a2981df7d62f447`（Tree
`8b1c8d968d6192c30ec45e42a5b4dad11c673a7f`）的 Agent、MCP 与 Action 收敛已完成：

- Graph 私有转发壳与 live escalation 为 `0`；Decision、Tool Loop、Evidence、Policy 与三 Action
  Pipeline 使用类型化单 owner，2 Round / 6 Attempt 预算和安全终态保持；
- 12 个默认入口为 `6839` 非空非注释行，核心决策函数不超过 `199` 行；Runtime Import Graph
  `206 modules / 2754 edges`，Forbidden Reachability `0`，SCC `0`；
- 能力分母精确为 `9 Read / 3 Proposal / 3 Runtime`，Action Schema SHA-256 为
  `b9508627d4959b332b082582b1cba31899c905d0b7934fc4168d7ce89fe70136`；
- `i200_baseline_0001 → i201_retire_escalation` 只产生 direct ACL 与 generic execute definition 两条
  Catalog Delta；direct escalation 返回 `42501`，retired / unknown capability 零写；
- Current Integration `299/299`，`source_dirty=false`，Source State SHA-256
  `acfc4cd3298022ffeeefaa263d42f6dc61ab8db789267c8c6d08ef62c7a5daac`；34 个历史节点和 2 个
  Dedicated 节点按 Manifest 排除，测试数据库已删除；
- MCP Hermetic `6/6` + PostgreSQL `11/11`，失败 `0`、跳过 `0`、孤儿进程 `0`；Manifest SHA-256
  `5734067851478475b6c0077599892c88fab84a73fd73d3c95fcd5c7a5bfbc4d7`；
- Hermetic Backend `2204/2204`；Frontend `5 files / 76 tests`，Lint 与 Build 通过；Ruff、Bandit、
  `uv lock --check`、`pip-audit`、Runtime Mypy `206` 与 Validation Mypy `79` 均通过；
- Invocation-owned PostgreSQL / Redis、Phase 4 数据库、Label-matched Container / Network / Volume、
  55435 / 56381 Listener 与 MCP / Runner 子进程残留均为 `0`。

Phase 4 durable Receipt：
`validation/evidence/interview_v2/phase4/phase4-validation-receipt.v1.json`，SHA-256
`f2ab7fa5b742bc56b6d0e96ae4933cfca9bd61e957901323f35cf53868b14a3d`。Database Retirement 原始载体位于
`dist/phase4/escalation-retirement-05f15760a97d05f55fa597456a2981df7d62f447.json`，SHA-256
`8944b672a2f925c682da3bdfc9b5a62b93d461314beb6aaae5fe1fd088316d34`。

Phase 5 Candidate `70717d8f19a9cbe3d8ead99db228c93f1577acc4` 的前端状态收敛已完成：

- Conversation 与 Approval 分别由 Query、Stream、Mutation 和 View State owner 组成；Conversation /
  Approval Page 为 `295 / 189` 行，均无资源级 `useState` / `useEffect` 或直接 API 调用；
- 初始 `/approvals` 始终以 Customer 身份安全启动，Approver 只能由显式用户动作切换；Tenant 切换会清空
  旧 Projection，路由只渲染服务端 `/session` 返回的真实角色；
- 长会话独立滚动、审批来源、Loading / Error / Empty、归档连接、键盘焦点、390px 移动端、离线恢复与
  远端审批结果自动对账均通过浏览器验收；
- Frontend `6 files / 81 tests`，Lint 与 Build 通过；Playwright `19/19`；Hermetic Backend
  `2248/2248`，另有 `397` 个 PostgreSQL / Redis / MCP 节点按合同排除；
- Runtime-only 镜像 Inventory / Smoke 通过，Backend / Frontend 镜像均绑定精确 Candidate；Clean Compose
  为 PostgreSQL、Redis、API、Dispatcher、Reconciler、Frontend、2 Worker 和 4 个 MCP 子进程；
- 默认 pinned GHCR transport 未直接验证；构建仅以 SHA-256
  `04792cac761c4a6ba78267f36f2af541b7f92196d42ac55d21d3ff6b0f5ab6a5` 固定的官方 Astral uv
  `0.11.2` GitHub Release 资产替代 builder 传输，最终镜像不包含该替代；
- 两个 invocation-owned Phase 5 项目的 Container / Network / Volume、四个 Host Port Listener、前后端
  Candidate 镜像与临时 transport 镜像残留均为 `0`。

Phase 5 durable Receipt：
`validation/evidence/interview_v2/phase5/phase5-validation-receipt.v1.json`，SHA-256
`bbbc1d13156604b6bef8b36ccbeacd5bcdb2050c045d391ffeb26a8f15d55d8a`。390px Golden Screenshot
SHA-256 为 `be64ca6ce7558ad6f0f9c06d8987b8ec52526942a500d6aa542c466a4394e16e`。

Phase 6 Candidate `30254587585fa2169cab071a926c501e06dac9a6`（Tree
`199ca61783c5857cc95f83a468f1b80a5a313d81`）的受控 Pruning 与 Authority Transition 已完成：

- SHA-256 Archive Transition Manifest 精确列出 `2,197` 个文件、`20,264,669` 字节；历史文档、旧
  Migration、旧 Evaluation 输入/报告、历史测试 Carrier、Validation Package 与脚本已从当前工作区
  迁出，当前只保留 8 份权威文档；Archive Tag 与 Phase 5 Evidence Head Source Commit 均验证可达；
- Test Disposition 记录 6 组历史 Oracle 的 current replacement，14 条 Safety Invariant 均保留 current
  owner；历史结果没有被重写，旧节点仍可从 Git source commit 恢复；
- Hermetic Backend `1315/1315`，另有 `241` 个 PostgreSQL / Redis / MCP 节点按合同排除；Current
  Integration `225/225`，`source_dirty=false`，Source State SHA-256
  `2dcd3c8cc6b5ddb7dd2edbe023dd97376ced49891e47f476b738fa850d9fe7c4`，数据库已删除；
- MCP Hermetic `6/6` + PostgreSQL `10/10`，失败、跳过与孤儿进程均为 `0`；Manifest SHA-256
  `cfbce7636341604843f139a4103c53dcc6dbe00f7683dd30316c4100b81b2d95`；
- Frontend `6 files / 81 tests`，Lint 与 Build 通过；Ruff、Bandit、`uv lock --check`、pip-audit、
  Runtime Mypy `204` 与 Validation Mypy `8` 均通过；
- Runtime / Validation wheel RECORD 为 `218 / 12`、路径重叠 `0`；Runtime Import Graph 为
  `204 modules / 2736 edges`，Forbidden Reachability 与 SCC 均为 `0`；
- 精确 Candidate 的 Runtime-only 镜像 Inventory / Smoke 通过；Runtime CLI 只暴露 6 个运行命令，
  Validation Module、Tests、Scripts 与源码路径泄漏均为 `0`；
- Invocation-owned PostgreSQL / Redis、隔离数据库、MCP / Runner 子进程、Runtime Candidate Image 与
  Buildx Builder 清理残留均为 `0`。

Phase 6 durable Receipt：
`validation/evidence/interview_v2/phase6/phase6-validation-receipt.v1.json`，SHA-256
`e73b22d8888ace2838e135eaa5ce28d180c7dba5e476e932eb0e57e0c219d1d9`。Archive Transition Manifest
SHA-256 为 `7a62d7c3141d8a6c1bfc6460393d0329b285061dbcb2f229a2ecbaa6d7645f7f`；Package Boundary 原始
Candidate 载体位于
`dist/phase2/package-boundary-30254587585fa2169cab071a926c501e06dac9a6.json`，SHA-256
`b5297e815f43638993af855367b656b321b121f3e70887cbfd92ef2a28207dfa`。

Phase 7 Candidate `b132c395c2edf2d7d72477dc9051bffc3d7f4024`（Tree
`78ed357459173ebb5354f24396fb42e96a22a98d`）已执行以下证据：

- RAG Dev30：eligible Recall@5 `26/30`（`0.866666...`），MRR@10 `0.753703...`，Citation Binding
  `115/115`，Conflict / Unanswerable Safety `10/10`，Unsupported Material Claim `0/115`；
- IE-F06 `6/6`，真实 Provider 调用 `0`，MCP orphan `0`，隔离数据库已删除；
- IE-J12 `12/12`，三条主 Web Demo 已包含；其底层证明为 Backend denominator `1562`、Current
  Integration `225`、MCP `16`、Frontend `81`、Browser `19` 与 Clean Compose `8`，均通过；
- Hosted CI Run `31633888433` 为 `completed_success`，不是本地结果替代；
- 同一 SHA 唯一一次真实 DeepSeek Native Tool Calling IE-P16 已完整执行 `16` 条：通过 `11`、失败
  `5`（IE-P02 / P03 / P04 / P09 / P10）。Safety Pass 为 `true`，Semantic Pass 为 `false`，真实
  external effect 为 `0`，最高实际估算费用 `¥0.349337`；所有场景 Project / Volume、具名 Builder
  与镜像清理通过。

失败 Receipt：
`validation/evidence/interview_v2/phase7/attempts/ie-p16-b132c395c2edf2d7d72477dc9051bffc3d7f4024.json`，
SHA-256 `68cf3f1d4c9bb8ade2fdca5b7b5d404cef3dc5822d751e34fbc416d245ec6bfa`。该 Receipt 不可改写，
旧 SHA 不得重跑。用户只授权一个 replacement Candidate；只有新 SHA 的零成本前置证明和 Hosted
CI 全绿后，才允许该新 SHA 消费一次完整 IE-P16。该历史授权随后已消费；用户之后另行授予后续
clean Candidate 与必要真实 DeepSeek 验证的持续授权。

唯一获批 replacement Candidate `7527c0acca079f57549538e49135a91ef87b9389`（Tree
`b9d96a0dd984cf8874a00f8f00172ac6f34db4be`）已执行以下证据：

- Package Boundary 为 `candidate_eligible=true`；Runtime / Validation wheel RECORD 为 `220 / 19`、
  零重叠，Runtime Import Graph 为 `205` Modules / `2755` Edges / `0` SCC / `0` Forbidden Reachability；
- Backend `1576`、失败/错误 `0`、预期 Skip `224`；Frontend Lint、`81/81` 与 Build 通过；
- Current Integration `225/225`，隔离数据库删除；MCP Hermetic `6/6` + PostgreSQL `11/11`，
  orphan `0`；Browser `19/19`，Flaky/Skip/Unexpected 均为 `0`；
- Runtime-only 镜像 Inventory / Smoke 通过，Runtime Distribution `224` files、零 Validation / Test
  泄漏，Revision 精确绑定 Candidate；Clean Compose 为 8 个实例、2 Workers、4 MCP Children，
  Embedding / Index Contract cohesive；
- RAG Dev30：eligible Recall@5 `26/30`、MRR@10 `0.753703...`、Citation Binding `115/115`、
  Conflict / Unanswerable Safety `10/10`、Unsupported Material Claim `0/115`；
- IE-F06 `6/6`；IE-J12 `12/12` 且包含三条主 Web Demo；
- Hosted CI Run `31664415941` 为 `completed_success`，5 个冻结 Job 共 `76` 步，全部成功；
- 同一 SHA 唯一一次真实 DeepSeek Native Tool Calling IE-P16 完整执行 `16` 条：通过 `13`、失败
  `3`。IE-P14、IE-P15、IE-P16 均记录 `scenario_execution_failed:ReadTimeout`。异常兜底因没有
  数据库用量快照而在原始 Receipt 写入 `0 / 0`，这不是三项实际 Provider 零调用证明；三项实际
  用量与超时 HTTP 阶段均未知。因此正式 Safety Pass 与 Semantic Pass 均为 `false`。真实 external
  effect 为 `0`，已观测 Prompt / Completion Token 总量为 `248121 / 14710`、对应 Receipt 估算费用
  `¥0.277541`，实际总用量和费用可能更高；所有场景 Project / Container / Network / Volume、具名
  Builder、镜像与 MCP 子进程残留均为 `0`。

replacement IE-P16 Receipt：
`validation/evidence/interview_v2/phase7/attempts/ie-p16-7527c0acca079f57549538e49135a91ef87b9389.json`，
SHA-256 `450a121f1bd77b8dd0beb9cb09a116ad0ba1993aee48f31917ce79f5f7f68e58`。Hosted Receipt：
`validation/evidence/interview_v2/phase7/hosted-ci-7527c0acca079f57549538e49135a91ef87b9389.json`，
SHA-256 `090e253cc4e2eb86167e240dc07a50bd18ad00d5aa6ce66562cfd95d72357eb0`。聚合 Receipt：
`validation/evidence/interview_v2/phase7/phase7-replacement-validation-receipt.v1.json`，SHA-256
`f470c557f61d17b6abf3866f2d56111b9a2c33e5d26978f8b03fbc9c144c6150`。三份 Receipt 均绑定精确
Candidate / Tree；两个已消费 SHA 均不得重跑或选择性重跑失败项。用户已持续授权后续 clean
Candidate 的通用修复与必要真实 DeepSeek 验证。

最终 Candidate `4466290963993e0b7662d75b571e4b15e4e97627`（Tree
`f4d021c13eac823d807cf3d120a99a610df9bb7b`）已执行以下证据：

- Source Identity 为 clean `HEAD == origin/main`，Source State SHA-256
  `d59800aca5116b1c3cb3f4a33115a98b0f6b24d758da0f55082f17adbed098cc`，共 `580` 个 Source File；
- 首次并发 Package Boundary 尝试因本地基础设施超时失败，失败载体原样保存；同一未变 Candidate
  的顺序重试通过。Runtime / Validation wheel RECORD 为 `221 / 20`、重叠 `0`；Runtime Import
  Graph 为 `205` Modules / `2758` Edges / `0` SCC / `0` Forbidden Reachability；
- Backend `1592/1592`，失败/错误 `0`、预期 Skip `224`；Frontend `81/81`，Lint 与 Build 通过；
- Current Integration `225/225` 且隔离数据库已删除；MCP Hermetic `6/6` + PostgreSQL `11/11`，
  orphan `0`；Browser `19/19`，Flaky / Skip / Unexpected 均为 `0`；
- Runtime-only 镜像 Inventory / Smoke 通过，Runtime Distribution 为 `225` files；Clean Compose
  为 `8` 个实例、`2` 个 Worker、`4` 个 MCP Child，Embedding Contract cohesive；
- RAG Dev30：eligible Recall@5 `26/30`、MRR@10 `0.753703...`、Citation Binding `115/115`、
  Conflict / Unanswerable Safety `10/10`、Unsupported Material Claim `0/115`；
- IE-F06 `6/6`；IE-J12 `12/12` 且包含三条主 Web Demo；
- Hosted CI Run `31687980408` 为 `completed_success`，冻结 5 个 Job 共 `76` 步，全部成功；
- 同一 SHA 唯一一次真实 DeepSeek Native Tool Calling IE-P16 完整执行 `16/16` 且全部通过。
  Prompt / Completion Token 为 `265737 / 21560`，Provider Usage 无未观测场景，完整估算费用为
  `¥0.308857`；Safety、Semantic、Usage、Cleanup 与 Complete Matrix Claim 全部为 `true`，真实
  external effect 为 `0`；
- Scenario Container / Network / Volume、Candidate Image 与 MCP 子进程残留均为 `0`；共享 Docker
  Daemon 不属于本次 Invocation，未声明为已清理。

最终 IE-P16 Receipt：
`validation/evidence/interview_v2/phase7/attempts/ie-p16-4466290963993e0b7662d75b571e4b15e4e97627.json`，
SHA-256 `21186631e6525743f1d1a617fe0e181500c9d2e1841531a355be500aa0ad45b5`。Hosted Receipt：
`validation/evidence/interview_v2/phase7/hosted-ci-4466290963993e0b7662d75b571e4b15e4e97627.json`，
SHA-256 `1c82310915018a88f29762adba797ed0c97ed833208c11451d31e413de42c6b3`。聚合 Receipt：
`validation/evidence/interview_v2/phase7/phase7-final-validation-receipt.v1.json`，SHA-256
`5dc7be8398169fb65dc265faec5a33e19caf20acd03f5df950c238c511b519f0`。三份 Receipt 均绑定精确
Candidate / Tree。该 SHA 已消费且不得重跑；Phase 7 机器验证与工程 DoD 已完成，最终 DoD 只等待
用户 Human Acceptance。

## Hosted CI 历史处置与当前执行

Phase 7 Candidate `b132c395c2edf2d7d72477dc9051bffc3d7f4024` 的 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard-Interview/actions/runs/31633888433> 已完成且结论为
`success`；`hosted_execution_started=true`、`local_execution_used_as_substitute=false`、
`release_blocker=false`。这关闭了该 Candidate 的 Hosted 前置条件，但不覆盖其 IE-P16 语义失败。

Phase 7 replacement Candidate `7527c0acca079f57549538e49135a91ef87b9389` 的 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard-Interview/actions/runs/31664415941> 已完成且结论为
`success`；冻结 5 个 Job（backend / integration / frontend / product-e2e / image）共执行 `76` 步，
全部成功。分类为 `completed_success`，并明确 `hosted_execution_started=true`、
`local_execution_used_as_substitute=false`、`release_blocker=false`。这关闭了 replacement 的 Hosted
前置条件，但不覆盖其 IE-P16 `13/16` 失败。

Phase 7 最终 Candidate `4466290963993e0b7662d75b571e4b15e4e97627` 的 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard-Interview/actions/runs/31687980408> 已完成且结论为
`success`；冻结 5 个 Job（backend / integration / frontend / product-e2e / image）共执行 `76` 步，
全部成功。分类为 `completed_success`，并明确 `hosted_execution_started=true`、
`local_execution_used_as_substitute=false`、`release_blocker=false`。这关闭了最终 Candidate 的
Hosted 前置条件，不覆盖也不改写两个历史 Candidate 的 IE-P16 失败事实。

- Run URL：<https://github.com/Teddy-creator/SupportGuard/actions/runs/31512749202>；
- Candidate：`72ea297e466d77b68a75f007f12bc0cdeabca41b`；
- Receipt：`validation/evidence/interview_v2/phase1/hosted-ci-receipt.v1.json`；
- Receipt SHA-256：`bfe9595ed751c9a12d6fc84445a84c9bd6feaff674f006e7300f918d91f581fd`；
- 分类：`external_zero_step_blocker`；
- 五个已发现 Job 的总步骤数为 `0`，账户 Payment / Spending Limit 注释证明 Runner 未启动；
- 本地运行没有被用作 Hosted CI 替代品。

按 v2.0 Phase 1 合同，该外部 Release Blocker 已登记且不阻止进入 Phase 2～6；这里只记录 Phase 1
时点没有 Hosted CI 绿色声明。最终 Candidate 的 Run `31687980408` 已真实执行并关闭当前 Hosted
前置条件，历史零步骤事实仍保留且不被改写。

Phase 2 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard/actions/runs/31520751057> 同样发现冻结的 5 个 Job
全部为 `0` 步骤；Recorder 以退出码 `2` 登记 `external_zero_step_blocker`，并明确
`hosted_execution_started=false`、`local_execution_used_as_substitute=false`、
`release_blocker=true`。这不会撤销 Phase 2 本地闭环，也不构成 Hosted CI 通过。

Phase 3 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard/actions/runs/31538133178> 同样发现冻结的 5 个 Job
全部为 `0` 步骤；账户 Payment / Spending Limit 注释证明 Runner 未启动。Recorder 退出码为 `2`，
分类为 `external_zero_step_blocker`，并明确 `hosted_execution_started=false`、
`local_execution_used_as_substitute=false`、`release_blocker=true`。Durable Hosted Receipt 为
`validation/evidence/interview_v2/phase3/hosted-ci-receipt.v1.json`，SHA-256
`ce4d3c206bbe012ba7f1fb2b768deb4392a6de32463c581b6e2bcd52dca5e8da`；Raw Artifact SHA-256 为
`9077ed494ca374665d65b14b89a6dd5ea23eafe0b318b769657736e7f5b2cbc3`。这里仍没有 Hosted CI
绿色声明。

Phase 4 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard/actions/runs/31560564403> 同样发现冻结的 5 个 Job
全部为 `0` 步骤；账户 Payment / Spending Limit 注释证明 Runner 未启动。Recorder 退出码为 `2`，
分类为 `external_zero_step_blocker`，并明确 `hosted_execution_started=false`、
`local_execution_used_as_substitute=false`、`release_blocker=true`。Durable Hosted Receipt 为
`validation/evidence/interview_v2/phase4/hosted-ci-receipt.v1.json`，SHA-256
`0be059f31e855a5336b84720428fd375ed22ec8e1fead3f20acc4feead2d2c7f`。这里仍没有 Hosted CI
绿色声明。

Phase 5 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard/actions/runs/31565826533> 同样发现冻结的 5 个 Job
全部为 `0` 步骤；账户 Payment / Spending Limit 注释证明 Runner 未启动。Recorder 退出码为 `2`，
分类为 `external_zero_step_blocker`，并明确 `hosted_execution_started=false`、
`local_execution_used_as_substitute=false`、`release_blocker=true`。Durable Hosted Receipt 为
`validation/evidence/interview_v2/phase5/hosted-ci-receipt.v1.json`，SHA-256
`137939f829d4dff07c2454f48514d2b1d26135af633bb2617aa70f8945d9a886`。这里仍没有 Hosted CI
绿色声明。

Phase 6 Hosted CI Run
<https://github.com/Teddy-creator/SupportGuard/actions/runs/31573174199> 同样发现冻结的 5 个 Job
全部为 `0` 步骤；账户 Payment / Spending Limit 注释证明 Runner 未启动。Recorder 退出码为 `2`，
分类为 `external_zero_step_blocker`，并明确 `hosted_execution_started=false`、
`local_execution_used_as_substitute=false`、`release_blocker=true`。Durable Hosted Receipt 为
`validation/evidence/interview_v2/phase6/hosted-ci-receipt.v1.json`，SHA-256
`6bdb72e7b60ca994b561df1b88c7738acffedffe0d15f740b8a5902c07e1a41e`。这里仍没有 Hosted CI
绿色声明。

## 当前尚未执行或尚未完成

- 用户 Human Acceptance：不看答案完成 15 分钟主讲；随机 10 问至少 `8/10`；随机一条 Demo 在
  5 分钟内定位入口、关键类型、数据库终态与失败路径。机器证据不能替代作者所有权；
- Evaluation v6 Holdout、Cross-Encoder A/B、真实外部 Effect 与生产 SLA：不在 v2.0 范围内，
  未执行且不会宣称完成。

## 证据归属规则

1. 每个阶段只引用自身精确 SHA 与实际命令结果；
2. Hosted CI 必须保存 Run URL、Candidate SHA、Job 名称、步骤数与结论；本地运行不能替代；
3. 零步骤的 Billing / Spending Limit / Actions 权限 / Runner 配额问题登记为外部 Release
   Blocker，不写成测试失败或测试通过；
4. 完整 IE-P16 对每个 Candidate 恰好运行一次；失败即消费该 Candidate，后续只能由通用修复形成
   新 clean SHA 后再运行完整矩阵；
5. 最终 Candidate 必须绑定 Prompt、Provider Schema、Tool Schema、ActionSpec、Corpus / Index、
   Embedding、Seed、Migration Head 与 Git Tree Hash。
