# Video Replacer

一个由 Codex Agent 负责下载、完整配置和运行的视频替换项目。用户给 Agent 一条安装提示词；同一个任务从下载连续完成全部依赖、本地马赛克能力、持久视频后端、登录和实时在线验证。此后用户只需描述替换要求，仓库内的局部 Skill 会整理素材、完成本地准备，并且只在用户明确批准当前批次后提交付费生成。

实现支持目标是 macOS Apple Silicon 和原生 Windows 11 x64：两者都由同一个 Agent 任务从 clone 连续安装到实时 `READY`。Windows 使用根目录 `.cmd` 入口和 OpenAI 官方 modern native standalone `codex.exe` package，不依赖 WSL、Git Bash、npm 的 `codex.cmd` / `codex.ps1` wrapper，也不要求永久改变 PowerShell execution policy。Linux 构建与测试路径保留为 preview，但不承诺账号连接后的完整安装。

Windows 上仅有 `.exe` 后缀不构成来源证明。仓库在任何 Codex 执行前校验 canonical `packages/standalone/releases/` package layout 与 `codex-package.json`，从元数据确定精确版本，再只读取 `https://releases.openai.com/codex/releases/<version>/release.json` 中唯一的 `codex-x86_64-pc-windows-msvc.exe` SHA-256，并重新计算实际 `codex.exe` 的摘要。非官方或旧 layout、tag/asset/摘要不匹配、离线或无法取得精确官方 manifest 时均 fail closed，不执行该二进制，也不能进入 `READY`。摘要随已安装的精确官方版本解析，不把某个版本或 digest 写死为永久信任值。

Windows 的“公开发布已验证”是独立的发布硬 Gate，不由代码或 CI 存在自动推导。每个候选版必须按 [RELEASE.md](RELEASE.md) 在真实 Windows 11 x64 fresh-user 主机上留存验证证据；未通过前不得对外标记 Windows release-ready。本文描述的是实现合同，不声称当前候选版已完成该真机 Gate。

## 数据与费用边界

- `prepare` 先由确定性父流程用本地 FFmpeg 对当前 Job 的源视频做有界抽帧。隔离 Codex 回合只接收带时间戳抽帧、有序参考图和结构化 Job JSON；源视频本身不附加给该回合。这些图像和 JSON 会发送给用户当前配置的 OpenAI/Codex 服务。
- 该 Codex 回合使用 `CODEX_EXEC_SERVER_URL=none`，`execution environment` 为 `none`，不注册执行或文件系统工具。它只返回结构化提示词字符串；父流程校验后才写入 `prompt.txt`。
- 提示词回合使用一个稳定的外置节点专用 `CODEX_HOME`：它固定使用经过严格 schema 校验的 file auth，不包含 `AGENTS.md`、rules、skills、plugins、MCP、hooks 或其他用户配置。首次 setup 时 Agent 通过 `setup login-codex-node` 发起该专用登录，用户只在 Codex 的受保护界面完成账号授权。Doctor 与生产回合始终使用同一个 home，不从外层 Agent 每次复制 auth。显式登录修复只会在 home 结构、安全属性和文件身份均通过检查后清除一个格式错误的普通 `auth.json`；有效 auth 保留，链接、非常规文件或不安全 home 会 fail closed。
- 原生 Windows 的外置 state root 固定为 Known Folder API 从真实当前用户 token 解析的 `FOLDERID_LocalAppData/video-replacer`；`LOCALAPPDATA` 等环境文本不能改变它，`VIDEO_REPLACER_STATE_DIR` 只能等值重述这一 canonical path，不能重定向凭证或账本。UNC、mapped drive、non-fixed drive、reparse 路径和任意其他 override 均 fail closed。节点 home、`auth.json` 和认证锁位于该 root，三者的 owner 为当前用户，DACL 禁用继承，只保留当前用户与 `SYSTEM` 两个 full-control ACE；setup 负责加固，Doctor 和生产前重新验证。
- `READY` 还要求同一稳定认证锁内的两阶段本机 zero-tool wire attestation。Doctor 两次都使用 exact production command、同一个节点 home、合成图片和 loopback Responses provider：第一阶段 `requires_openai_auth=false`，要求有图片、顶层/附加 tools 均空、无 multi-agent hints 且无 auth header；第二阶段 `requires_openai_auth=true`，在保持相同 zero-tool 图像 request surface 的同时捕获一次认证握手，并用内存中的 SHA-256 与 `hmac.compare_digest` 证明 Bearer 值等于严格校验后的稳定 `auth.json` `access_token`。两阶段各由 HTTP 418 立即停止，原始 token/header 不保留也不记录。它能发现 managed/system 配置把 MCP、hook、tool 或 Agent 指令重新注入实际 wire surface。
- 所有提示词节点 Codex 进程串行执行，避免多个 Job 并发刷新或改写同一个 file-auth token store。
- `prepare` 不向视频后端上传媒体，也不创建付费任务。
- `--confirm-paid submit-prepared` 会把最终上传文件发送到公开 setup 已验证的 Dreamina 后端，并可能消耗账户积分或产生费用。
- `mosaic_required` 只是自动马赛克处理，不保证完整覆盖或不可逆匿名化。
- 源媒体、参考图、运行状态和输出都在 Git ignore 边界内；恢复与防重账本位于仓库外的系统 state 目录。

## 用 Agent 下载并安装

把仓库 URL 填入 [AGENT-INSTALL-PROMPT.md](AGENT-INSTALL-PROMPT.md)，再把整段交给本机 Codex。短版如下：

```text
请下载这个仓库，并在同一个任务中立即把完整环境配置好。你负责检测平台，在 macOS Apple Silicon 使用根目录无后缀入口，在原生 Windows 11 x64 使用根目录 .cmd 入口和 OpenAI 官方 modern native standalone codex.exe package；Windows 不得改用 WSL/Git Bash、npm codex.cmd/codex.ps1 wrapper 或永久改变 PowerShell execution policy。在任何仓库触发的 Windows Codex 执行前，必须校验 modern package layout/codex-package.json，并用其中的精确版本从 https://releases.openai.com 对应 release.json 取得 codex-x86_64-pc-windows-msvc.exe SHA-256 后核对实际 binary；旧或非官方 layout、摘要不匹配、离线或无法证明来源时 fail closed，不得以固定版本或 digest 代替实时来源验证。安装全部依赖和本地马赛克能力，创建稳定外置、instruction-free、strict-file-auth 的节点专用 CODEX_HOME 并通过 setup login-codex-node 引导我完成 Codex 账号授权；Windows state root 必须由真实当前用户 FOLDERID_LocalAppData 固定解析为 LocalAppData/video-replacer，拒绝环境伪造和重定向，home/auth/lock 必须位于本机 fixed drive，拒绝 UNC、mapped/non-fixed drive 与 reparse 路径，并使用仅当前用户和 SYSTEM 拥有 full control 的 protected DACL。让 Doctor 在同一稳定锁内用 exact production command 完成两阶段本机 zero-tool loopback wire attestation：先验证 requires_openai_auth=false 的带图、零 tools/agent hints、无 auth 请求，再验证 requires_openai_auth=true 的相同零工具认证握手与稳定 auth.json access_token 相符；原始 token/header 不保留或记录。安装仓库受审的项目内 Dreamina CLI，引导我在服务自己的界面完成必要登录，并持续修复直到当前平台的 video-replacer setup status --json 实时返回 ready: true。不要等第一个视频任务再配置，也不要让我运行命令或手工编辑配置；不要上传媒体、创建付费任务、消费积分或启动后台服务。
```

如果仓库已经下载，只需在 Codex 中打开仓库并发送同一段话。Codex 会从 Git 根读取 `AGENTS.md`，并发现 `.agents/skills/video-replacer/` 中的局部 Skill。

下载文件本身不会绕过操作系统静默执行代码。“下载即配置好”的产品合同是：下载与配置属于同一个不间断的 Codex 安装任务，Agent 在 clone 后立即继续到 `READY`。用户只批准具体系统/网络变更，并在 Dreamina 自己的界面完成登录；Agent 负责其余命令和修复，不要求用户编辑 JSON、env 或 profile。

安装完成的唯一判据是 Agent 用当前平台 launcher 查到的实时状态：macOS 使用 `./video-replacer setup status --json`，Windows 使用 `.\video-replacer.cmd setup status --json`。关键字段为：

```json
{
  "schema_version": 3,
  "ready": true,
  "backend_profile": "dreamina_cli_seedance_2_5",
  "backend": "dreamina",
  "capabilities": ["mosaic_required", "video_replacement"],
  "live_checks": "pass"
}
```

该状态来自当前时点的在线 Doctor 验证，并写入 ignored 的 `.video-replacer/setup.json`。Doctor 会直接验证生产使用的同一个外置节点 `CODEX_HOME`、严格 file-auth schema、登录与模型可用性，并在同一认证锁内用上述两阶段 loopback capture 验证 exact production request surface；无认证阶段对应 `codex-wire`，认证摘要阶段对应独立 required check `codex-file-auth-wire`，任一失败都会阻断 READY。记录绑定代码合同、受审 Dreamina 二进制 SHA-256、工具路径与身份、后端 profile 和当前已检查的本地能力，但不包含账号凭证；Windows Codex identity 还保存当前已证明的 `official_sha256` 和 `official_release_tag`。每次 live `setup status` 都重新校验 modern package layout/metadata、重新取得该精确版本的官方 release manifest 并重算实际 `codex.exe` SHA-256，而不是只信 setup record、文件名后缀或一次缓存的 digest；生产提示词进程也在执行前重复同一来源 Gate。离线或无法重新证明官方来源会返回 `ready: false`。此后每次任务前还会重查节点 Codex 登录/模型、Dreamina 登录、CLI 暴露的模型能力、账号等级和其余工具；环境或仓库变化会由 Agent 修复。两阶段捕获的模型请求都指向本机 loopback mock，携带合成图片并在 HTTP 418 停止，因此没有模型响应或推理。认证阶段只在内存中比较摘要，不保存或记录原始 access token、Bearer header 或请求 header 值。该 wire 证据不判断进程的其他网络活动；独立的登录、模型 catalog 与 Dreamina 检查仍按各自在线合同运行。Wire 不执行真正的抽帧提示词回合，不证明某个具体批次能够准备成功，也不证明付费生成已可成功完成。Dreamina 的无付费接口没有提供一次真实模型推理的 entitlement probe，因此这里的 Seedance 2.5 就绪证据是“受审 CLI 支持 + 已登录 + 非基础账号等级”，真正提交仍保留独立付费 Gate。安装过程不会启动 watcher。

## 第一个任务

把源视频和参考图放在任意本地位置，然后直接告诉仓库中的 Agent。macOS 例如：

```text
把 ~/Movies/source.mp4 的车内饰替换成 ~/Pictures/interior.png，人物保持不变，无需打码。
先只准备，不要付费提交。
```

Windows 可以直接给原生绝对路径，包括空格和中文：

```text
把“C:\Users\example\Videos\测试素材\source video.mp4”的车内饰替换成“C:\Users\example\Pictures\参考图\interior.png”，人物保持不变，无需打码。
先只准备，不要付费提交。
```

Agent 会把素材复制进一个新的 `workspace/video-loop/inbox/<batch>/`，写入 schema-v3 绑定并运行本地 `prepare`。只有类似下面这种针对当前批次的明确指令，才授权提交：

```text
确认付费提交刚才准备好的这个批次，并等到下载完成。
```

自动人脸马赛克依赖已属于首次完整安装和 `READY` 检查，不会延迟到第一条需要打码的视频。

缺少参考图时，自动生成参考图只在当前 Codex 客户端具备图像生成能力时可用；否则 Agent 会要求用户提供文件。

## 运行目录

```text
video-replacer/
├── .agents/skills/video-replacer/   # Codex 自动发现的操作 skill
├── .video-replacer/setup.json       # Agent 验证的本地后端选择，不入 Git
├── assets/reference-images/         # 本地复用参考图库，内容不入 Git
├── workspace/video-loop/            # 批次状态机，全部不入 Git
├── outputs/video-replacements/      # Prompt、preflight 与最终视频，不入 Git
├── tools/                            # 控制面、状态机、adapter、安装与检查器
└── workflows/                        # 详细合同
```

防重/恢复 state 和节点专用 Codex home 不在仓库内。节点 home 固定为平台外置 state 边界下的 `codex-node-home/`，不随临时节点 staging 一起删除。POSIX 可继续使用绝对 `VIDEO_REPLACER_STATE_DIR` override；原生 Windows 固定使用真实当前用户 `FOLDERID_LocalAppData/video-replacer`，同名变量只能等值重述这个 canonical root。Windows 的该目录只允许本机 fixed drive；home、`auth.json` 和认证锁均使用 owner 为当前用户、仅当前用户与 `SYSTEM` full control、继承受保护的 DACL。

根入口由 Agent 按平台选择，不要让下载者转录命令：

| 用途 | macOS / Linux preview | 原生 Windows 11 x64（PowerShell 或 Command Prompt） |
| --- | --- | --- |
| 安装 | `./install --with-mosaic` | `.\install.cmd --with-mosaic` |
| workflow | `./video-replacer` | `.\video-replacer.cmd` |
| 测试 | `./video-replacer-test` | `.\video-replacer-test.cmd` |

下面以平台的 `<LAUNCHER>` 表示 `./video-replacer` 或 `.\video-replacer.cmd`。常用的无费用命令是：

```text
<LAUNCHER> status
<LAUNCHER> once
<LAUNCHER> check <batch>
<LAUNCHER> prepare <batch>
```

付费命令必须在用户明确批准当前批次后单独运行：

```text
<LAUNCHER> --confirm-paid submit-prepared <batch>
```

## 后端

首次安装时，Agent 发现、配置并在线验证一个可跨 Agent session 使用的后端。首个公开 READY 路径是 Dreamina CLI；用户不需要填写 `backend_profile`。Skill 从本地 setup record 读取对应 profile，同一批次只使用这一个已验证 profile：

| Profile | 后端 | 上传上限 | 本地转换目标 |
| --- | --- | ---: | ---: |
| `dreamina_cli_seedance_2_5` | 即梦 CLI / Seedance 2.5 | 200,000,000 bytes | 190,000,000 bytes |
| `dreamina_cli_seedance_2_0` | 即梦 CLI / Seedance 2.0 | 50,000,000 bytes | 47,000,000 bytes |

Agent 可以用既有 Dreamina CLI 判断账号是否已授权，但公开 READY 始终使用仓库内的受控安装器下载官方固定版本、校验 SHA-256，并只执行 ignored 的 `.video-replacer/bin/dreamina`（Windows 为 `dreamina.exe`）。任意 PATH/自定义 CLI 不能写入 READY。需要登录时，Agent 只通过平台 launcher 的 `setup login-dreamina` 子命令发起，不直接执行 provider 二进制。安装器不会运行官方远程 shell 安装脚本、修改 PATH 或安装全局 Skill。

Ark API adapter 代码保留用于内部开发和测试，但当前环境变量密钥方案不能提供跨 Agent session 的持久安装，也缺少可信的无付费账号/模型能力 probe，因此不属于公开 launcher/operator 路径，也不会被 `setup verify` 标记为 READY。未来只有加入受审 credential broker 与完整 probe 后才进入同一安装合同。[.env.example](.env.example) 仅是开发者字段合同，不是下载者安装步骤。

## 开发与发布

开发者在 macOS / Linux preview 使用 `./video-replacer-test`，在 Windows 使用 `.\video-replacer-test.cmd`。发布审计由对应虚拟环境的 Python 运行 `tools/release_audit.py`。

架构与安全合同见 [ARCHITECTURE.md](ARCHITECTURE.md)、[SECURITY.md](SECURITY.md) 和 [workflows/video-replacement-batch-folder-loop-v1.md](workflows/video-replacement-batch-folder-loop-v1.md)。贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，发布检查见 [RELEASE.md](RELEASE.md)。项目使用 [Apache-2.0](LICENSE.txt)。
