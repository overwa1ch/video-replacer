<!-- project-worktree-boundary: v1 -->

# Video Replacer Repository

本仓库是可独立安装和运行的 Video Replacer 项目。用户要求下载、安装、配置、首次运行、准备、替换、打码、生成参考图、提交、恢复或跟踪视频替换任务时，完整读取 `.agents/skills/video-replacer/SKILL.md`，只通过当前平台的根 launcher 操作 workflow：macOS / Linux preview 使用 `video-replacer`，原生 Windows 11 x64 使用 `video-replacer.cmd`。

## Runtime boundary

- Git 跟踪区：skill、workflow、工具、合同、示例和测试。
- 本地批次：`workspace/video-loop/`，全部 ignored。
- 本地产物：`outputs/video-replacements/`，全部 ignored。
- 可复用参考图：`assets/reference-images/`；只跟踪 README 与空 catalog 示例。
- 防重、恢复、节点 staging 和节点专用 Codex home：平台外置 state 目录，不得移动进仓库。
- 凭证：Dreamina 留在自身 CLI credential store；提示词节点使用 setup 专门创建的稳定外置、instruction-free、strict-file-auth `CODEX_HOME`。任何凭证均不得写入仓库、batch、manifest、日志或回复。Windows state root 通过 Known Folder API 从真实当前用户的 `FOLDERID_LocalAppData` 固定为 `LocalAppData/video-replacer`；忽略伪造的 `LOCALAPPDATA`，`VIDEO_REPLACER_STATE_DIR` 只能等值重述 canonical root。Windows 的 home、`auth.json` 和认证锁只允许本机 fixed drive，使用 owner 为当前用户、仅当前用户与 `SYSTEM` full control、继承受保护的 DACL；重定向、UNC、mapped/non-fixed drive 与 reparse 路径 fail closed。

源码职责：

- `.agents/skills/video-replacer/`：对话 Agent 的唯一操作合同。
- `tools/video_batch_orchestrator.mjs`：唯一控制面。
- `tools/video_batch_loop.py`：状态机、父层本地 FFmpeg 抽帧、共享节点 file-auth home 的 Codex 进程串行化、无执行/文件系统工具的节点回合、父层 `prompt.txt` 持久化、素材绑定、自动打码、媒体准备和确定性 Gate。
- `tools/backend_profiles.py`：受控后端与模型 profile 注册表。
- `tools/dreamina_video.py`、`tools/ark_video.py`：只能由注册 profile 选择的远端 adapter。
- `tools/video-replacement-node-contracts/`：隔离 Video-to-Prompt 观察回合合同；只接收父层附加的抽帧、参考图和结构化 JSON，不读 skill 或项目文件。
- `tools/bootstrap.py`、`tools/doctor.py`：无登录、无付费的安装与就绪检查。
- `tools/codex_artifact.py`：在执行 Windows Codex 前验证 modern standalone package layout/metadata，从精确官方 release manifest 取得摘要并校验实际 binary；来源无法证明时 fail closed。
- `tools/codex_wire_attestation.py`：在同一稳定认证锁内用生产 prompt command、稳定节点 home、合成图片与 loopback Responses provider 捕获两阶段实际 request surface；无认证阶段 required check 为 `codex-wire`，认证摘要阶段 required check 为 `codex-file-auth-wire`。认证阶段以仅驻留内存的 SHA-256 与 `hmac.compare_digest` 证明 Bearer 等于严格 file-auth `access_token`；任一阶段不满足时阻断 READY。
- `tools/install_dreamina.py`：从仓库受审 manifest 下载并校验项目内 Dreamina CLI；不执行远程 shell、不改 PATH、不安装全局 Skill。
- `tools/dreamina_environment.py`：所有 Dreamina 子进程共用的凭证最小化环境；Windows 仅在 child process 内使用可信系统目录 PATH，阻断 pinned CLI 无超时的 bare-name PowerShell/CIM ancestry probe，不修改用户或父进程 PATH。
- `tools/setup.py`：首次运行的后端选择、工具身份、代码合同与实时在线 Doctor 证明；只保存 ignored 的非敏感 setup record。
- `tools/release_audit.py`：公开文件边界检查。

不要把 runtime、媒体、密钥、外置状态账本或真实任务记录写入 Git。新 workflow 功能先加确定性测试，再改文档。普通对话任务不启动常驻 watcher。

## First-run setup Gate

用户要求下载、安装、配置或第一次运行本项目，或视频任务开始前当前平台 launcher 的 `setup status` 不是 `READY` 时，完整读取上述 skill 与其中的 first-run reference，由同一个 Agent 从下载连续完成整个安装。不要把环境配置延迟到第一个视频任务，也不要让用户运行命令或手工编辑配置；用户只处理必要的系统授权和服务自身的 OAuth/device login。

Windows 必须使用原生 `.cmd` 入口、Windows 路径语义和 OpenAI 官方 modern native standalone `codex.exe` package；npm 的 `codex.cmd` / `codex.ps1` wrapper 不能进入 READY。`.exe` 后缀不是 trust signal。仓库在任何 Windows Codex 执行前必须验证 canonical `packages/standalone/releases/` layout 与 `codex-package.json`，从元数据取得精确版本，只从 `https://releases.openai.com/codex/releases/<version>/release.json` 接受唯一 `codex-x86_64-pc-windows-msvc.exe` SHA-256，并重算实际 binary。旧或非官方 layout、tag/asset/摘要不匹配、离线或无法证明来源时 fail closed，不执行 binary；不得把某个版本或 digest 写死成永久信任值。setup identity 保存 `official_sha256` 与 `official_release_tag`，每次 live status 重新获取 manifest 并重算。

不得将 WSL 或 Git Bash 当成安装前提，不得要求永久改变 PowerShell execution policy。原生 Codex 的外层仓库 Agent 与 workflow 可使用官方 `windows.sandbox="elevated"`；该名称表示 Codex 的隔离模式，不表示整个 Agent 进程以系统管理员身份运行。内层提示词回合固定使用首次 setup 创建的稳定外置、instruction-free、file-auth `CODEX_HOME` 和 `CODEX_EXEC_SERVER_URL=none`；它不依赖沙箱执行环境，也不创建第二套 elevated sandbox/home。沙箱初始化或一次性系统软件安装所需的 UAC/管理员批准必须经用户确认并限定范围；独立管理员安装进程结束后，在已配置的 Codex 沙箱内完成 READY。含空格或中文的仓库路径不得被重写、简化或转移。

首次安装必须完成本地 runtime、马赛克依赖、一个可跨 Agent session 复用的受支持后端、节点专用 Codex strict-file-auth 登录、在线 Doctor 和 ignored setup record。Agent 只通过 launcher 的 `setup login-codex-node` 发起节点专用登录，用户只完成 Codex 账号授权；Doctor 与生产使用同一个外置 home。显式登录只在安全 home 结构/ACL 与稳定文件身份通过后修复一个格式错误的普通 `auth.json`；有效 auth 保留，链接、非常规文件或不安全 home 在 Codex 启动前 fail closed，登录结束后必须通过严格 schema。Doctor 还必须在同一稳定锁内用 exact production command、该 home 和合成图片完成两阶段本机 zero-tool wire attestation：`requires_openai_auth=false` 阶段只接受带图、顶层/附加 tools 均空、无 multi-agent hints 且无 auth header 的 request；`requires_openai_auth=true` 阶段接受相同 zero-tool surface，并验证唯一 Bearer 与稳定 `auth.json` `access_token` 的内存摘要相符。两阶段捕获的模型请求都必须路由到 loopback mock，以 HTTP 418 在模型响应或推理前停止，且不保存或记录原始 token/header；该 wire 证据不判断进程的其他网络活动。实际 wire surface 若被 managed/system MCP、hook、tool 或 agent instruction 重新注入，READY 必须失败。首个公开 READY 路径固定使用由仓库 manifest 和 SHA-256 验证的项目内 Dreamina CLI；PATH 中的任意 CLI 和环境变量密钥型 Ark adapter 不得伪装成持久 READY。达到当前平台 launcher 的 `setup status --json` 实时 `ready: true` 才能结束安装或创建批次。`READY` 不执行真正的抽帧提示词回合，不上传媒体、不创建付费任务或消费积分，也不保证具体批次准备或付费生成成功。安装不启动 watcher。Dreamina 登录只能通过 launcher 的 `setup login-dreamina` 受控子命令发起，不直接执行二进制。

## Worktree Boundary

- 本仓库由所属 Project Shell 登记；coordinator 创建、分配、验证和移除 linked worktree。
- 并行 worker 只写 `git rev-parse --show-toplevel` 返回的当前工作树，不创建、移动或删除 worktree，也不编辑外层 Shell 管理文件。
- 提交或交回前运行下面全部验证命令。

平台统一入口先运行完整套件：macOS / Linux preview 使用 `./video-replacer-test`，Windows 使用 `.\video-replacer-test.cmd`。必要时再用当前 `.venv` 的 Python 和项目 Node.js 逐项复现失败；不要为测试切换到 WSL/Git Bash。
