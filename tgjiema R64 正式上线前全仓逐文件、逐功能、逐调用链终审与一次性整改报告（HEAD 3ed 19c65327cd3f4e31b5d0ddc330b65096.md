# tgjiema R64 正式上线前全仓逐文件、逐功能、逐调用链终审与一次性整改报告（HEAD 3ed31ca）

<aside>
⛔

**终审裁定：DEVELOPMENT PASS / STAGING CONDITIONAL GO / PRODUCTION NO-GO。** 当前源码质量和测试覆盖继续提升，但权威 Release Gates 仍失败；在本文 P0、P1 和生产证据门禁全部闭环前，不得正式上线。

</aside>

## 1. 报告范围与审查声明

- 仓库：[maxiuquan/tgjiema](https://github.com/maxiuquan/tgjiema)
- R64 HEAD：`3ed31cabcf808cdbd1f0d0637d18986937d0c3b4`
- Tree：`13a03da3948ebac84723412234a05caa2bd605cc`
- R63 基线：`5d3f66549cd9833351eb1d3319e95b7adfdd0eb7`
- 提交时间：2026-07-18 22:59:05（Asia/Shanghai）
- 提交签名：GitHub verified / valid
- 本轮提交统计：15,915 additions、1,780 deletions、17,695 total changes
- 审查对象：源码树、关键数据与安全调用链、四套 GitHub Actions、数据库迁移、备份恢复、outbox、MFA、按钮安全、错误协议、中英语言包、无障碍、CRDB 空载 RU 策略。
- 审查原则：提交说明只能证明整改意图，不能证明问题关闭；关闭依据必须是实际实现、负向测试和成功的生产门禁。

> “一次性发现所有问题”在工程上应理解为：一次性列出本次静态审查与现有运行证据能够确认的全部问题，并给出可执行闭环方案。任何报告都不能诚实保证未来不存在未知缺陷、依赖漏洞或真实环境差异。
> 

## 2. Actions 权威证据

| 工作流 | Run | 结果 | 终审解释 |
| --- | --- | --- | --- |
| Deploy Check | 29649084014 | Success | 部署配置基础检查通过 |
| CI | 29649083926 | Success | 单元、静态与回归门禁通过 |
| E2E Tests | 29649083919 | Success | 当前 E2E 集合通过 |
| Release Gates | 29649083937 | **Failure** | 生产发布链仍不成立 |

Release Gates 的确定性失败链：

- `verify-branch-protection` job `88092206036`：`Verify branch protection is configured` 失败。
- `rc-continuity` job `88092206038`：三次连续候选证据门禁失败。
- `sign-image` job `88092269929`：keyless OCI signing 成功，Fulcio SAN 提取成功，但紧随其后的 `Verify image signature (output verification statement)` 仍失败。
- 因镜像验证失败，SLSA provenance、source artifact signing、migration manifest signing/verification、release commit binding、signed artifacts upload 均 skipped。
- 因上游失败，最终 release summary/attestation 不可能形成完整可部署证据链。

**结论：**普通测试通过不能覆盖 Release failure。当前产物没有形成“同一镜像 digest → 签名 → SBOM → provenance → attestation → deployment”的闭环。

## 3. R63 问题关闭矩阵

### 已实质改善

- `VerifiedBackupPayload` 已递归 deep-copy/freeze，并在 writer 入口按实际 payload 重算摘要。
- CLI 已删除旧 `get_latest_backup()` 双 loader，改为 `backup_id → COMPLETE → manifest → payload`。
- restore capability nonce 已从纯进程内集合迁移到持久化原子消费接口。
- MFA 新增唯一异步权威 verifier，包含签名、年龄、SQLite 吊销查询和一次性消费。
- report/delete 回调已将 expected action、audience 作为 verifier 必填参数；主要举报反馈改为 `UserMessage`。
- 004 migration 已进入 manifest 列表，并增加重复行取证描述。
- observability fingerprint 已不再直接依赖行号。

### 仅部分关闭或尚未关闭

- Release 签名验证仍失败，因此供应链 P0 未关闭。
- restore 仍依次修改 CRDB、cache SQLite、relay SQLite，不具备跨存储原子切换。
- outbox 增加 fail-fast 和 lease recovery，但未证明生产 provider 注册、真实启动入口、续租、DLQ 与 compensation 全链闭合。
- migration manifest 在提交树中仍绑定旧 SHA；CI 临时重生不能替代可部署产物绑定。
- `UserMessage` 有了安全适配器，但不能证明所有真实 sink 已禁止裸字符串。
- 高风险按钮仍未形成统一 MFA、双人审批和资源版本策略。
- observability 违规仍通过大规模 allowlist 延迟处理，并未归零。

## 4. P0：上线阻断问题

### P0-01 Release Gates 仍失败

**事实：**签名成功后立即验证失败；分支保护和 RC continuity 同时失败。

**整改逻辑：**

1. 对 cosign 使用实际签名产物进行诊断，但验证策略不能信任待验证证书自身提供的任意 identity。身份必须同时满足仓库、工作流路径、事件、ref/commit 和 OIDC issuer 的独立预期策略。
2. 将 `cosign verify --json` 的可信验证结果保存为 statement；从验证结果核对 image digest、certificate SAN、issuer、workflow ref、commit SHA。
3. 修正 `--output-verification` 与当前 cosign 版本的实际 CLI 契约；在固定 cosign SHA/版本下增加最小复现测试。
4. branch protection 检查必须使用具备只读 administration 权限的专用 token；401/403、缺 context、规则集不一致均 fail-closed，并输出明确原因。
5. RC continuity 只统计同一 release policy、同一 protected branch、完整 required jobs success 且无 skipped 的连续运行。

**验收：**连续三次 Release Gates success；所有 required job 均实际执行，无 skipped；同一 digest 完成签名、SBOM、provenance、attestation 和部署验证。

### P0-02 提交树中的 migration manifest 不是当前 release trust root

**事实：**仓库文件仍声明：

- `release_commit = 3513de91224844c83aa3370a8267ce7a6590ed5e`
- `tree_sha = bd8337d0f6e710f1dc734c1163eaa40b239924de`

而 R64 是 HEAD `3ed31cab...` / Tree `13a03da...`。CI 中“运行测试前重生工作区文件”只能改变 runner 的未提交文件，不能自动证明 Docker 镜像、部署包和运行时读取的是同一份已签名 manifest。当前 Release 又在 manifest 签名前失败。

**整改逻辑：**

1. 不要把会改变 tree 的 manifest 直接绑定到包含自身的 tree，避免自引用循环。生成独立 release artifact manifest，绑定 source commit、source tree、migration file digest 集合和 image digest。
2. 先 checkout 固定 commit，计算 source tree 与迁移集合；构建一次镜像并取得 digest；生成 canonical release manifest；再对 manifest 和镜像 digest 分别签名并产生 attestation。
3. 镜像内部保存 canonical manifest 及其签名材料，启动时必须验证：manifest signature、source commit、image digest、迁移集合完全一致。
4. `MIGRATION_MANIFEST_VERIFY=0` 只能存在于显式 `APP_ENV=local|test`；staging/production 未启用验证必须拒绝启动。
5. 非 git 部署环境不能“warning 后继续”。必须从签名 attestation 或镜像 label 获取 source commit/tree 并强制比对。

**负向测试：**旧 HEAD、少一个 migration、多一个 migration、修改 SQL 一字节、替换证书、其他工作流签名、无 git 目录、未设置验证变量均应阻断。

### P0-03 restore 仍可能形成跨数据源混合时间点

**事实：**writer 仍按 CRDB → cache SQLite → relay SQLite 顺序执行。前一数据源成功、后一数据源失败时，无法用普通事务回滚已经提交的另一个存储。覆盖模式尤其可能先清空生产表再失败。

**整改逻辑：**

1. 恢复只能写入全新的 staging CRDB database/schema 与新的 SQLite 文件，禁止原地覆盖生产。
2. 每个数据源完成 schema、行数、主外键、业务守恒、抽样/全量 hash 和应用只读演练。
3. 写入持久化 `restore_operations`：operation_id、backup_id、manifest digest、阶段、每个数据源状态、验证摘要、审批/MFA、切换版本。
4. 所有 staging 数据源均验证成功后，在维护窗口执行版本化蓝绿切换；任何失败只销毁 staging，不影响 active 数据。
5. 切换后保留旧版本作为限时回滚点；回滚也必须使用状态机和审计事件。
6. nonce 不应在真正写入前永久消费。采用 operation ledger：验证后 `reserved`，成功切换后 `consumed`，失败后允许同 operation 安全重试但禁止换 payload。

**验收：**对 CRDB、两个 SQLite 分别在 0/25/50/75/100% 注入故障，active 数据始终保持单一一致时间点；三次全新空白环境完整恢复成功。

### P0-04 outbox 尚无生产闭环证据

**事实：**新 worker 在生产模式缺 provider 时会 fail-fast，这是进步；但 `validate_providers()` 在导入关键 effect 集合失败时返回空列表，属于 readiness fail-open。`test_mode=True` 仍允许无 provider 后 complete。尚未看到所有九类 effect 的生产 provider、进程启动、长任务 lease renewal、DLQ 告警和补偿执行证据。

**整改逻辑：**

1. 删除“导入异常返回空列表”；任何 registry/schema 加载异常直接 readiness failure。
2. 生产构建从代码层移除 no-provider-complete 分支；测试使用独立 fake provider，不允许 worker 静默完成。
3. 使用枚举型 provider registry，并在启动时比较 `required_effect_types == registered_types`；缺失、多余、重复均拒绝就绪。
4. provider 接收 immutable `OutboxEnvelope(event_id, effect_type, target, request_hash, idempotency_key, payload_digest)`；外部系统必须按 idempotency key 去重。
5. lease 使用 fencing token/版本号；complete、fail、renew 都必须 CAS `event_id + owner + lease_version + request_hash`。
6. provider 调用超过租期三分之一即自动续租；续租失败立即停止提交结果。
7. DLQ 必须告警并产生可审批 replay；compensation 通过独立 outbox 执行，不能只保留 Python callback。
8. 旧 `services/outbox_worker.py` 必须删除或完全迁移；未知 event type 必须进入 DLQ，严禁标记成功。

**验收：**多 worker kill -9、网络超时、成功响应丢失、重复投递、lease 过期、未知事件、provider 缺失等故障注入下，不丢事件、不重复不可幂等副作用、不静默 complete。

### P0-05 高风险操作策略仍不统一

**事实：**ban 进入审批，但 detach/block/delete_file 仍存在“不需审批”的显式设计；签名按钮只解决 token 完整性和部分绑定，不能替代 MFA、双人审批、业务授权和目标资源版本校验。

**整改逻辑：**

- 建立单一 `HighRiskPolicy`：`action → required_role、MFA、two_person、reason、resource_version、cooldown、reversible、outbox_effects`。
- delete、ban、detach、block、restore、purge、密钥轮换、权限变更默认 `MFA + requester != approver + resource version CAS`。
- callback token 必须绑定 tenant、actor、audience、exact action、sub_action、resource id、resource version、locale、session id、expiry、nonce。
- handler 不再自行决定风险级别，只能构造命令并交给 policy/CommandBus。
- 对“取消/忽略”等低风险按钮也绑定 actor/session/resource，防止跨会话误操作。

**验收：**自动枚举所有 destructive handler；任何一个绕开 CommandBus/policy、缺 MFA/审批/version binding，门禁失败。

## 5. P1：正式上线前必须关闭

### P1-01 deep freeze 仍应改为单一 canonical bytes 来源

`MappingProxyType + tuple` 减少 Python 对象篡改，但 payload 与 tables 仍是两个独立字段，存在语义分叉风险；`json.dumps(..., default=str)` 还可能把不支持类型静默字符串化。

- 验证对象只保存 `canonical_payload_bytes`、digest 和解析后的只读 view。
- `tables` 必须从已验证的同一 bytes 解码，不接受调用方独立传入。
- 禁止 `default=str`；只允许 JSON schema 声明类型，NaN/Infinity、bytes、自定义对象全部 fail-closed。

### P1-02 capability nonce 的权威存储选择仍需明确

若 nonce ledger 落在正在被恢复的 SQLite 数据库中，恢复/文件替换可能回滚消费记录；多实例若使用本地 SQLite，也不是真正共享权威层。

- ledger 应放在独立、不会被恢复覆盖的 CRDB security schema 或外部强一致存储。
- 对 `nonce` 建唯一键并绑定 backup_id、manifest digest、operation_id；记录 reserved/consumed/failed 状态。

### P1-03 migration 004 必须用实际 SQL 证明严格守恒

manifest 的说明宣称 duplicates evidence 与严格等式已实现，但说明文字不是证明。

- 对 original、strict、quarantine、duplicates 四组使用稳定 row identity；断言每个原始 row_id 恰好出现一次。
- `original_count = strict_count + quarantine_count + duplicate_evidence_count`。
- 保存原始 payload hash、冲突组、保留行、淘汰原因；迁移回滚/重跑必须幂等。

### P1-04 outbox 唯一冲突必须按数据库错误类型分类

不得通过错误字符串包含 `unique` 或 `constraint` 判断幂等成功。应检查 SQLite error code/constraint name；仅指定幂等唯一索引冲突可视为已存在，CHECK/FK/NOT NULL 必须回滚并报警。

### P1-05 MFA 旧 verifier 仍可被生产代码调用

异步权威 verifier 已加入，但 deprecated 并不等于禁止。

- 将 sync verifier 改为私有密码学 primitive；生产模块只能导入 Protocol 暴露的 async authoritative API。
- CI 用 AST/import graph 阻止生产目录直接调用旧 verifier。
- `consume=False` 只能由 UoW 内部 capability 调用，并强制在同事务 CAS 消费。

### P1-06 中英语言包与用户消息边界未完全闭合

- 已修复举报主流程的多处硬编码中文，这是有效进步。
- 但 Telegram/FastAPI/WebSocket/SSE/email/template 的原始 API 仍天然接受 str；新增 `render_for_send()` 不能证明所有调用点均经过它。
- `query.message.text + localized_message` 会继承旧消息语言，切换 locale 后可能形成混合语言。
- 将内部异常 `str(result.error)` 作为翻译参数可能暴露实现细节。

**一次性整改：**

1. 各出口建立 typed adapter，业务模块禁止直接导入第三方 send/edit/response API。
2. AST 门禁从“已知 sink 列表”升级为 import-boundary：只有 adapter 包可调用原生 sink。
3. 语言包校验包括 key 对称、ICU AST、变量集合/类型一致、复数规则、禁止中文泄漏到 en-US、禁止英文泄漏到 zh-CN。
4. Release 模式语言包缺失、解析失败、变量不匹配一律 fail-closed，不能 fallback 后继续。
5. 错误展示只传 safe params 和 trace_id；内部 exception 仅进入结构化日志。

### P1-07 统一错误码仍存在 281 级别的存量债务模式

当前 baseline 改用了更稳定的 AST 指纹并按模块给出计划，这是进步；但大规模 allowlist 仍代表问题存在，不是 `real_violations=0`。报告中不得把“全部匹配 allowlist”写成“无违规”。

- 上线目标：security/destructive/data-integrity/financial 为 0；observability 存量也应清零。
- 按模块把 `except Exception: pass`、裸 False/0、裸 ValueError、裸用户字符串转换为明确 ErrorCode、ErrorEnvelope 和 metric。
- fingerprint 使用 AST 规范化结构与 qualified symbol；源代码文案变化不应创建新身份。
- 每个错误码必须定义 HTTP/Telegram presentation、retryability、retry button、audit level、safe params、对应中英 key。

### P1-08 按钮式流程的可用性与无障碍仍不完整

- 每个 destructive action 必须先显示目标、影响范围、不可逆性、审批状态和取消按钮。
- Telegram 按钮标签需中英一致、避免仅 emoji 表意；错误后提供可聚焦/可操作的重试或返回按钮。
- Web 端按钮必须有 accessible name、键盘焦点、disabled/busy 状态、确认对话框焦点陷阱和结果 live region。
- token 失效、资源版本冲突、审批过期、MFA 过期均应回到可恢复流程，而不是死端文字。

### P1-09 a11y success 不能替代完整矩阵证明

E2E 已通过，但上线还需强制：expected test count 与实际 executed 完全相等、任何 skip/xpass 为失败，并覆盖 zh-CN/en-US、键盘、屏幕阅读语义、错误态、loading、empty、分页、模态框、动态按钮、权限不足、审批与 MFA 全状态。

### P1-10 CRDB 空载 RU 尚无真实证据

代码目标不等于账单结果。应保持：

- bot 进程空载不得连接 CRDB；`CRDB_POOL_MIN_SIZE=0`、按需 max 1–2、空闲立即回收。
- 所有轮询改为 Redis/SQLite/event wakeup；禁止空载 health query、定时 COUNT、TTL job、leader election heartbeat 命中 CRDB。
- backup 默认使用 Cockroach 托管备份或低频增量/变更流，不做应用层周期性全表扫描。
- RU telemetry 按 SQL fingerprint、service、job、时间段归因；超过 100 RU/day 告警，超过 500 RU/day 阻断发布。

**验收：**同一生产配置连续 72 小时无人使用，bot 角色 0 RU/day，集群理想 ≤20 RU/day、硬上限 ≤100 RU/day；有用户时 ≤250 RU/DAU/day；月度预算 ≤35M RU。

### P1-11 分支保护门禁本轮无法通过

Release 已明确失败。必须在 GitHub ruleset/branch protection 中确认 21 个 required contexts 与当前 workflow job 名完全一致，禁止管理员绕过，要求 PR、至少一名独立 reviewer、dismiss stale approvals、conversation resolved、signed commits；用于检查配置的 token 仅有读取权限。

### P1-12 生产运行证据缺失

- 缺 7 天多实例 soak。
- 缺三次全新空白环境恢复录像/日志/摘要。
- 缺真实 provider outbox 故障注入。
- 缺 72h RU 证据。
- 缺同 digest 完整供应链证据。

## 6. 国际化、统一错误码、按钮式流程专项验收表

| 专项 | 当前状态 | 上线条件 |
| --- | --- | --- |
| zh-CN / en-US | 部分通过 | key、ICU、参数类型、真实英文、双语言 E2E 全通过 |
| UserMessage | 核心类型已增强 | 所有生产 sink 只能经 typed adapter |
| 统一错误码 | schema 有进步，仍有 allowlist 债务 | 高风险域和 observability 存量归零 |
| 按钮签名 | action/audience 绑定改善 | 增加 tenant/session/resource version/MFA/审批策略 |
| 按钮 UX | 部分实现 | 确认、取消、重试、过期恢复、双语言、无障碍状态齐全 |
| Web a11y | E2E 当前通过 | 无 skip，全路由全状态双语言矩阵 |

## 7. SDLC 八阶段终审

1. **需求：条件通过**
    - 功能范围清晰，但必须把 RTO/RPO、RU SLO、高风险操作矩阵、语言与无障碍验收写成可测需求。
2. **架构：不通过生产门槛**
    - 多数据源恢复缺蓝绿原子切换；outbox/provider 与 release trust root 仍未闭环。
3. **设计：条件通过**
    - capability、CommandBus、UserMessage 方向正确；需统一 policy、operation ledger 和 typed adapters。
4. **实现：条件通过**
    - 多项 R63 缺陷真实修复；仍有 fail-open、旁路和注释大于实现的问题。
5. **验证：条件通过**
    - CI/E2E 成功；Release 失败，真实环境、并发、故障注入和 RU 证据不足。
6. **发布：不通过**
    - branch protection、RC continuity、签名验证、attestation 链失败。
7. **运维：不通过生产门槛**
    - 缺 outbox DLQ/reconcile 实际运行证明、restore 演练和 72h RU 基线。
8. **退役与持续改进：条件通过**
    - 必须删除旧 restore/outbox/verifier 旁路，制定密钥轮换、数据保留、回滚和依赖升级策略。

## 8. 一次性整改执行顺序

### Wave 0：立即冻结发布

- 阻断 production deploy；保留 staging 测试。
- 修复 branch protection、RC continuity、cosign verify 三个 Release 失败点。

### Wave 1：数据与副作用安全

- 实现 staging restore + 蓝绿切换 + operation ledger。
- 完成生产 outbox provider、fencing lease、DLQ、replay、compensation。
- 将 destructive action 全部纳入统一 MFA/双人审批策略。

### Wave 2：供应链与迁移

- 改造非自引用 release manifest；绑定 source、migration set、image digest。
- 运行时强制 cosign verify，生产禁止关闭。
- 完成 004 严格逐行守恒测试。

### Wave 3：国际化、错误码、按钮和无障碍

- typed sink adapters + import-boundary gate。
- 清零错误协议债务。
- 双语言 ICU/参数/真实翻译检查。
- 全状态按钮 UX 和 a11y 矩阵，无 skip。

### Wave 4：生产证据

- Release 连续三次全绿。
- 三次空白环境恢复。
- 7 天多实例 soak 与故障注入。
- 72h 空载 RU 观测。
- 安全回归、渗透、依赖/SBOM/镜像扫描与回滚演练。

## 9. Production GO 最终门禁

仅当以下条件全部满足，裁定才能从 NO-GO 改为 GO：

- [ ]  本报告全部 P0、P1 有代码、测试、reviewer 和运行证据。
- [ ]  Release Gates 连续三次 success，无 required job skipped。
- [ ]  同一 image digest 完成签名、SBOM、provenance、attestation、部署验证。
- [ ]  branch protection/ruleset 检查成功，无法绕过 required contexts。
- [ ]  restore 在三次全新环境成功，故障注入不产生部分恢复。
- [ ]  outbox 无静默 complete、无丢事件、无重复不可幂等副作用。
- [ ]  所有高风险操作完成 RBAC、MFA、双人审批、资源版本和审计绑定。
- [ ]  中英语言包、统一错误码、按钮全流程和无障碍矩阵全部通过。
- [ ]  7 天多实例 soak 无一致性、安全和资源泄漏问题。
- [ ]  72h 空载 RU 达标并有可归因证据。
- [ ]  回滚、密钥轮换、告警、值班和事故响应演练完成。

<aside>
✅

**R64 最终意见：**本轮整改确实关闭或改善了深冻结、CLI 发现链、持久化 nonce、MFA 权威验证、按钮 action/audience 绑定和部分国际化问题；但 Release 仍失败，且 restore 原子性、供应链 trust root、outbox 生产闭环和统一高风险策略尚未达到商用上线标准。完成上述整改并取得全部生产证据后，才可重新申请上线终审。

</aside>