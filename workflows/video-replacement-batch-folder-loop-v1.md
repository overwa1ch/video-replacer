---
title: Video Replacer Canonical Workflow
type: workflow
status: LOCAL_SKILL_OPERATED / SAMPLED_READONLY_VIDEO_TO_PROMPT / PROFILED_UPLOAD_PREPARATION / EXPLICIT_PAID_SUBMISSION
updated: 2026-08-13
---

# Video Replacer Canonical Workflow

## 完整流程

用户只与 Video Replacer Project Agent 对话。项目局部 skill 是 Agent 的操作手册，`tools/video_batch_orchestrator.mjs` 是唯一 workflow 入口。

```text
同一下载任务中的 Agent setup 安装并检查当前合同所需的本地前置
→ 通过 setup login-codex-node 配置稳定外置的节点专用 strict-file-auth CODEX_HOME，在同一稳定锁内以共同 production zero-tool request surface 完成两阶段本机 wire attestation：无认证阶段追加 ephemeral credential-store 隔离覆盖，认证阶段保留 production file-auth 与摘要校验；随后配置持久 Dreamina CLI 并写入实时 READY record
→ Skill 从 READY record 读取 backend_profile，整理 schema-v3 批次与每个 Job 的 privacy_mode
→ 父流程用本地 FFmpeg 对当前源片做有界、带时间戳抽帧
→ 无执行/文件系统工具的 Video-to-Prompt 回合只接收抽帧、绑定参考图和结构化 JSON
→ 回合返回结构化提示词字符串，父流程重建规范素材绑定、插入不可变 Job 需求并通过语义 Gate 后写 `prompt.txt`
→ mosaic_required 时自动生成打码输入
→ 检查真正准备上传的视频，必要时做本地上传准备
→ 本地 probe 与 submission plan
→ 本地准备完成并等待当前批次授权
→ 显式付费提交到所选 adapter
→ 恢复或轮询远端任务
→ 下载文件
→ COMPLETED 或 blocked/PARTIAL
```

确定性父流程读取当前 Job 源视频，用受信 FFmpeg 生成从开头帧起、按时间均匀分布的有界顺序抽帧，并校验时间戳、大小与 SHA-256。受支持后端的源片最长 30 秒，当前以 0.75 秒节奏取样；对异常更长输入，有界策略也会把帧均匀分布在全程，而不是只消耗在片头。源视频本身不附加给 Codex 回合。回合只接收父层附加的抽帧、有序参考图和结构化 Job JSON，在一次模型调用中完成观察与提示词写作。它不写文件；父流程用登记数据重建素材绑定，把 `requirements.txt` 中该 Job 的原始文字解析并替换素材句柄后插入最高优先级区块，再把节点内容放进独立的模型逐镜说明区，并要求所有绑定语义名称都出现在该区。该 Gate 通过后才持久化 `prompt.txt`。不会产生中间分析产物交给第二个模型，也不创建画面质量审查、隐私效果复核或生成结果审查任务。

## 路径合同

| 角色 | 路径 |
| --- | --- |
| `REPO_ROOT` | clone 后的 Video Replacer Git 仓库根目录 |
| 首次安装记录 | `$REPO_ROOT/.video-replacer/setup.json`；ignored 且不含凭证 |
| 批次运行根 | `$REPO_ROOT/workspace/video-loop` |
| 固定 Job 产物根 | `$REPO_ROOT/outputs/video-replacements` |
| 防重、恢复和节点临时运行根 | POSIX 使用平台默认或绝对 `VIDEO_REPLACER_STATE_DIR` override；Windows 固定为真实当前用户 `FOLDERID_LocalAppData/video-replacer`，同名变量只能等值重述 |
| 节点 Codex home | `$VIDEO_REPLACER_STATE_DIR/codex-node-home/`；稳定、instruction-free、strict-file-auth，Doctor 与生产共用；Windows 要求本机 fixed drive 与私有 DACL |

Git 跟踪区只保存 skill、workflow、执行器、合同和测试。批次、媒体、日志、生成结果、凭证与外置账本保留在 ignored 或外置运行路径。

`LAUNCHER` 按平台固定解析：macOS Apple Silicon 和 Linux preview 为 `$REPO_ROOT/video-replacer`，原生 Windows 11 x64 为 `REPO_ROOT\video-replacer.cmd`。Agent 把 launcher 与参数作为数组直接调用，不拼接 shell 字符串，因此含空格或中文的仓库路径不会被拆分。Windows 合同不依赖 WSL/Git Bash，也不要求永久改变 PowerShell execution policy。

## 职责边界

| 层 | 职责 |
| --- | --- |
| 用户 | 首次安装时完成必要账户授权或受保护凭证输入；提供任务要求；批准当前批次的付费提交。 |
| Project Agent + local skill | 在同一下载任务中立即安装依赖与马赛克能力、配置持久后端并取得当前 setup 合同的实时 READY；整理可信输入；按需创建参考图；从 setup record 读取受控 profile；写 schema-v3 `job-bindings.json`；调用控制面；等待；简略汇报。 |
| JavaScript 控制面 | 命令入口、互斥、shadow 边界、当前批次付费 capability、进程等待和恢复调度。 |
| Python 父层 | 批次索引、输入哈希、显式 `privacy_mode`、受信本地 FFmpeg 抽帧、抽帧/参考图附加、节点 Codex 进程串行化、规范绑定与不可变需求组装、素材语义覆盖校验、`prompt.txt` 持久化、自动打码、上传准备、preflight、submission plan 和任务防重。 |
| 单个隔离回合 | 单 Job sampled Video-to-Prompt：只根据父层附加的抽帧、有序参考图和结构化 JSON 返回提示词字符串；无执行/文件系统工具，不持久化文件。 |
| adapter registry | 将冻结 profile 绑定到审核过的即梦 CLI 或 Ark adapter；不接受批次指定的可执行文件、模型或压缩参数。 |

批处理回合只接收当前阶段的内联合同和父层附加图像。`CODEX_EXEC_SERVER_URL=none` 使 `execution_environment` 固定为 `none`，Codex 不注册执行或文件系统环境；shell、patch、文件读取、权限请求和网络工具均不可用。回合使用 setup 专门创建的稳定外置 strict-file-auth `CODEX_HOME`；该 home 不包含 `AGENTS.md`、rules、skills、plugins、MCP、hooks 或其他用户配置。Windows 的统一 resolver 通过 Known Folder API 从真实当前用户 token 固定 state root 为 `FOLDERID_LocalAppData/video-replacer`，忽略伪造的 `LOCALAPPDATA`，且只接受等值重述 canonical root 的 `VIDEO_REPLACER_STATE_DIR`。Windows home、`auth.json` 与认证锁只允许本机 fixed drive，拒绝重定向、UNC、mapped/non-fixed drive 和 reparse 路径，并使用 owner 为当前用户、仅当前用户与 `SYSTEM` full control、继承受保护的 DACL。显式登录只在锁内通过安全结构/ACL 与稳定文件身份检查后清除格式错误的普通 `auth.json`，随后必须通过严格 file-auth schema；有效 auth 保留，链接、非常规文件或不安全 home fail closed。Doctor 直接验证生产使用的这一 home，而不为每次回合复制外层 auth，并在同一稳定锁内用共同的 production zero-tool request surface、合成图片和 loopback Responses provider 捕获两阶段最终 request：`requires_openai_auth=false` 阶段在生产命令末尾追加 `cli_auth_credentials_store="ephemeral"` 隔离覆盖并写入 required check `codex-wire`，只允许图片、零顶层/附加 tools、零 multi-agent hints 和零 auth/账号路由 header；`requires_openai_auth=true` 阶段保留 production file-auth 命令并写入 required check `codex-file-auth-wire`，要求相同 zero-tool surface，并用内存 SHA-256 与 `hmac.compare_digest` 证明 Bearer 等于稳定 `auth.json` `access_token`。两阶段捕获的模型请求都路由到 loopback，并由 HTTP 418 在模型响应或推理前停止；原始 token/header 不保留或记录。该 wire 证据不判断进程的其他网络活动，使用的只是合成图片且不包含视频后端或付费命令；managed/system 配置重新注入 MCP、hook、tool 或 Agent hint 时阻断 READY。所有节点 Codex 进程串行，避免并发刷新或改写 file-auth token。

## 新批次合同

下载完成后立即运行 first-run setup，不等待新批次。新批次开始前，`[LAUNCHER, "setup", "status", "--json"]` 还必须通过当前工具身份、代码合同、Codex 模型、严格 file-auth、`codex-wire`、`codex-file-auth-wire`、Dreamina 登录和马赛克能力的实时检查并返回 `ready: true`。Windows status 还须通过 Known Folder API 重新确认真实当前用户 canonical state root、拒绝环境重定向，验证节点 home 的 fixed-drive/private-DACL 边界、modern standalone layout/`codex-package.json`，从精确 `releases.openai.com` release manifest 取得 `codex-x86_64-pc-windows-msvc.exe` 摘要并重算实际 binary；任一边界失败、旧/非官方 layout、摘要不匹配或离线无法证明来源时 fail closed。Agent 使用 `[LAUNCHER, "setup", "profile"]` 返回的 profile；不要求用户选择或填写内部 profile ID。任务命令在 setup 未完成、失效或批次 profile 不匹配时 fail closed。Dreamina OAuth/device login 只由 `[LAUNCHER, "setup", "login-dreamina"]` 发起，Agent 不直接执行 provider 二进制。Windows 上安装、Doctor、登录/账户检查与 submit/query adapter 调用都复用同一个 child-only Dreamina 环境：PATH 只含 Win32 API 解析出的 native system directories，并设置 `NoDefaultCurrentDirectoryInExePath`，让 pinned CLI 1.4.15 的 bare-name PowerShell/CIM ancestry probe 立即失败；它不修改用户或父进程 PATH，也不构成通用子进程沙箱。

Agent 在隐藏 staging 目录中关闭并检查文件后，原子移动到：

```text
$REPO_ROOT/workspace/video-loop/inbox/<batch>/
├── videos/
├── replacements/
├── requirements.txt
├── job-bindings.json
└── PAUSE
```

输入文件必须是复制得到的普通文件，批次内 basename 唯一。源视频名前缀固定自然顺序，便于稳定分配 `V001…V###`。每个 Job 都有一条需求；参考素材的数组顺序就是 `@图片1…N` 顺序。

新批次必须使用 schema v3：

```json
{
  "schema_version": 3,
  "batch_id": "<batch>",
  "backend_profile": "dreamina_cli_seedance_2_5",
  "jobs": [
    {
      "id": "V001",
      "privacy_mode": "none",
      "references": [
        {
          "relative_path": "replacements/<file>",
          "semantic_name": "<自然名词短语>"
        }
      ]
    }
  ]
}
```

Agent 只提交 `schema_version`、`batch_id`、批次级 `backend_profile`、`jobs[].id`、`jobs[].privacy_mode`、`jobs[].references`、`requirements.txt` 和已复制媒体。Agent 不提交 `should_compress`、`compression_policy`、视频大小、限制大小、压缩参数或最终上传路径。输入校验会拒绝这些额外字段。

`privacy_mode` 只有两个值：

- `none`：使用 workflow 选择的普通源片输入。
- `mosaic_required`：准备阶段调用 `tools/privacy/face_mosaic.py`，并把独立打码文件设为后续上传准备的输入。

打码阶段只要求命令成功和输出文件存在。失败时当前 Job 阻塞，原片不会作为上传回退项。流程不检查命中数、画面覆盖度或打码效果。

旧 schema v1/v2 或旧提示词管线只允许恢复全 Job 已完成且提交计划可验证的冻结结果；任何半成品都必须重新整理 schema v3 新批次。重建时逐字复制旧批次的 `requirements.txt` 和未变化绑定，禁止为了通过节点或 Gate 改写硬约束。新 flow 绑定 `video-to-prompt-v3-parent-composed`、固定节点模型与当前节点合同 SHA-256，任一项变化后都拒绝续跑旧 flow。

## 受控 profile 与本地上传准备

Profile 保存服务端限制与格式约束，批次内所有 Job 使用同一 profile。

| Profile | 后端 | 硬限制 | 目标 | 本地选择 |
| --- | --- | ---: | ---: | --- |
| `dreamina_cli_seedance_2_5` | 即梦 CLI / Seedance 2.5 | 200,000,000 bytes | 190,000,000 bytes | 即梦 CLI 本地文件上传 |
| `dreamina_cli_seedance_2_0` | 即梦 CLI / Seedance 2.0 | 50,000,000 bytes | 47,000,000 bytes | 即梦 CLI 本地文件上传 |

参考视频技术约束由 profile 固定：MP4/MOV、H.264/H.265，存在音轨时为 AAC/MP3；发生转换时 workflow 写出 MP4/H.264/AAC。

上传准备的固定顺序是：

```text
源视频
→ 隐私打码（如需要）
→ 检查真正上传文件
→ 必要时压缩
→ 本地 probe
→ submission plan
→ 明确授权后上传
```

自动规则如下：

1. 文件未超限且格式兼容，直接使用，不重新编码。
2. 否则先尝试一次无损 remux；满足限制才使用该结果。
3. 仍不满足时，FFmpeg 最多做三轮两遍 H.264/AAC 重编码和码率修正。
4. 每轮根据最终实际字节数决定成败，不按预测码率放行。
5. 成品必须不超过 hard limit，能被 FFmpeg 完整解码，并满足所选 profile 的时长、分辨率和帧率等技术约束。
6. 无法达到要求、FFmpeg 缺失或验证失败时，Job 在上传前进入 `BLOCKED`，不会调用上传或付费提交。

原视频和打码视频永远保留。发生转换时输出 `source-upload-ready.mp4`；每次准备写 `upload-preparation.json`，记录 profile、`unchanged` / `remuxed` / `reencoded`、实际上传准备输入与最终文件的路径、SHA-256、字节数、限制/目标、FFmpeg 版本与参数、完整解码结果。该 Gate 只判断技术条件，不对画质做人工或 Agent 审查。

Ark adapter 当前只用于内部开发和测试；它没有持久 credential broker 与可信的无付费账号/模型 probe，因此公开 Skill、setup record 和根 launcher 不选择它。只有未来把这两项加入公开安装合同后，Ark profile 才能进入 operator 表格与批次 intake。

## 单回合抽帧视频到提示词

当前模型回合固定为 sampled Video-to-Prompt。父层先固定当前 Job 需求和有序参考素材绑定，再在本地用受信 FFmpeg 从开头帧起做有界、按时间均匀分布的顺序抽帧。父层校验帧文件、时间顺序、大小与 SHA-256，然后将抽帧、参考图和声明时长/时间戳/需求/绑定的 JSON 作为同一回合输入。抽帧是有界观察证据，不是完整视频语义的保证；无法从已给帧区分必需事实时，回合应返回 `BLOCKED` 而不是猜测。

回合使用 `CODEX_EXEC_SERVER_URL=none`，无执行、文件系统或网络工具，只根据已附加内容返回 schema 绑定的提示词字符串或精确 blocker。父层校验返回结果并独占写入 `prompt.txt`。该设计不产生、复用或传递中间分析 JSON；回合也不执行质量、适用性、身份一致性或生成结果审查。`COMPLETE` 只表示父层收到可校验的非空提示词；父层完成其余确定性 Gate、active video、上传准备、无费用 preflight 和 `submission-plan.json` 后，Job 才进入 `READY_FOR_SUBMISSION`。

父层 staging 目录位于外置 `node-runs/`。正常调用结束后删除一次性 staging，但稳定的节点 file-auth `CODEX_HOME` 保留；取得 workflow 独占锁后，启动清理只移除上次崩溃遗留的 `video-loop-*` staging 目录，记录目录名和数量。Windows 外层 Agent 与 workflow 可使用官方 elevated sandbox；内层无工具回合使用已登录的稳定节点 home，不依赖 sandbox 执行环境，也不创建第二套 elevated sandbox/home。父层在每个生产 Codex 进程执行前重新校验官方 modern package layout、精确 release tag/asset digest 和实际 binary SHA-256。

## 标准命令

### Shadow 扫描

```text
[LAUNCHER, "once"]
```

`once` 和 `watch` 固定为本地 shadow：可以接入、索引和记录本地状态，不上传、不创建远端任务。它们拒绝任何付费参数。

### 本地准备

```text
[LAUNCHER, "prepare", <batch>]
```

`prepare` 要求批次位于 `needs-input/` 且 `PAUSE` 存在。它运行分析、提示词、可选自动打码、上传准备、Gate、无费用 probe 和 submission plan；它不上传、不创建远端任务。

### 当前批次一次付费提交

```text
[LAUNCHER, "--confirm-paid", "submit-prepared", <batch>]
```

付费 capability 绑定当前 batch、冻结 flow、eligible Job 集合、profile、最终视频 SHA-256、上传准备 manifest SHA-256、preflight 和 submission plan。任何视频、manifest 或 profile 变化都会使旧 preflight 和授权失效；重新 `prepare` 后才可取得新的明确授权。已有 task ID 只恢复和轮询。

内部 Ark adapter 的测试合同仍要求付费 Gate 后才读取 Ark/TOS 环境，并使用私有 TOS、短期签名 URL、终态删除和自动过期兜底。它不是当前公开 operator 的可达分支；密钥、凭证和签名 URL仍不得写入批次文件、manifest 或日志。

Python 扫描 stdout 只输出一个 JSON；过程信息进入 stderr。JavaScript 在启动后续动作前完成 JSON 解析。

## 终态与恢复

全部 Job 下载成功且文件存在时，批次进入 `completed/`，Job 状态为 `COMPLETED`。部分成功、部分失败时，批次结果为 `PARTIAL` 并进入 `blocked/`，便于后续恢复失败项。全部失败同样进入 `blocked/`。

历史 `review/` 批次保留原位并可读取；新任务不写入 `review/`。

原进程中断后，使用 `status --json`、批次状态文件和外置账本判断恢复动作。已记录 task ID 的 Job 恢复同一任务；提交状态不确定时保持阻塞。

最终汇报只包含批次路径、Job 绑定、`backend_profile`、`privacy_mode`、本地准备或生成终态、生成文件路径、task ID 和精确 blocker。汇报不评价画面、人物一致性、打码覆盖效果或生成质量。

## Shadow automation

公开仓不安装或启用常驻服务。`watch` 只保留为用户显式启动的 shadow 命令，不携带付费参数；普通对话任务使用一次性 `once`、`prepare` 和 `submit-prepared`。
