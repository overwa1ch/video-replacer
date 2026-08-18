# Agent Install Prompt

把仓库 URL 替换进下面提示词，然后把整段交给本机 Codex。不要先手工下载依赖或配置后端。

```text
请安装并配置这个 Video Replacer 仓库：<REPOSITORY_URL>

把“下载”和“配置完成”视为同一个连续任务：下载后立即进入仓库，读取根 AGENTS.md 和仓库内 video-replacer Skill；由你检测并安装全部必需依赖（包括本地马赛克能力）和仓库受审的项目内 Dreamina CLI，通过平台 launcher 的 `setup login-codex-node` 创建稳定外置、instruction-free、strict-file-auth 的节点专用 Codex 登录，通过 `setup login-dreamina` 发起 Dreamina 账户登录，并修复所有检查失败。用户只在 Codex 和 Dreamina 自己的受保护界面完成账号授权；不要复制外层 Agent 的 auth，不要直接执行 provider 二进制，不要让我运行命令、编辑 JSON/env/profile 或自行排查环境。

先检测平台和架构。macOS Apple Silicon 调用根目录 `install`、`video-replacer` 和 `video-replacer-test`；原生 Windows 11 x64 调用根目录 `install.cmd`、`video-replacer.cmd` 和 `video-replacer-test.cmd`。Windows READY 只接受 OpenAI 官方 modern native standalone `codex.exe` package；不要安装或依赖 npm 的 `codex.cmd` / `codex.ps1` wrapper。`.exe` 后缀本身不是来源证明：在任何仓库触发的 Windows Codex 执行前，先验证 canonical `packages/standalone/releases/` layout 和该 release 的 `codex-package.json`，从元数据取得精确版本，再只从 `https://releases.openai.com/codex/releases/<version>/release.json` 读取唯一 `codex-x86_64-pc-windows-msvc.exe` 的 SHA-256，并核对实际 binary。非官方或旧 layout、tag/asset/摘要不匹配、离线或无法取得精确官方 manifest 时 fail closed，不得执行 binary 或宣告 READY；不要把某个当前版本或 digest 固定为永久信任值。若现有 Codex 不满足该合同，在获得明确系统/网络批准后按当前官方 Windows 安装方式安装 modern standalone package，再由仓库 launcher 验证。

Windows 上保留原生路径和参数边界，包括含空格或中文的仓库路径；不得改用 WSL/Git Bash，不得要求永久改变 PowerShell execution policy。原生 Codex 优先使用 `windows.sandbox="elevated"`；这里的 `elevated` 是 Codex 的首选隔离模式，不等于让整个 Agent 以系统管理员身份运行。若初始化该沙箱或安装可信系统软件需要 UAC/管理员批准，先说明具体变更并把批准范围限制到该步骤；独立的管理员安装进程结束后，回到已配置的 Codex 沙箱继续验证。

如果需要安装系统软件、联网下载可执行文件或登录第三方账号，先清楚说明具体变更并向我请求必要授权；我只负责批准系统变更和在服务自己的受保护界面完成登录。节点专用 Codex home 必须位于外置 state 边界，不得包含 `AGENTS.md`、rules、skills、plugins、MCP、hooks 或用户 config；Doctor 与生产必须共用它，并串行所有使用该 strict-file-auth store 的 prompt-node Codex 进程。Windows state root 必须通过 Known Folder API 和真实当前用户 token 固定为 `FOLDERID_LocalAppData/video-replacer`；不得信任 `LOCALAPPDATA` 等环境文本，`VIDEO_REPLACER_STATE_DIR` 只能等值重述这一 canonical path，任何重定向都必须 fail closed。Windows home、`auth.json` 和认证锁必须位于本机 fixed drive，拒绝 UNC、mapped/non-fixed drive 与 reparse 路径；owner 必须为当前用户，DACL 必须禁用继承并只允许当前用户和 `SYSTEM` full control。显式登录只可在安全结构/ACL 与稳定文件身份已验证后清除格式错误的普通 `auth.json`；有效 auth 必须保留，链接、非常规文件或不安全 home 必须在启动 Codex 前 fail closed。Doctor 必须在同一稳定锁内以共同的 production zero-tool request surface、同一 home 和合成图片完成两阶段本机 wire attestation：`requires_openai_auth=false` 阶段在生产命令末尾追加 ephemeral credential-store 隔离覆盖，只接受有图片、顶层/附加 tools 均空、无 multi-agent hints 且无 auth 或账号路由 header 的 request；`requires_openai_auth=true` 阶段使用 production file-auth 命令，要求相同 zero-tool surface，并以仅驻留内存的 SHA-256 和 `hmac.compare_digest` 证明 Bearer 等于严格校验后的稳定 `auth.json` `access_token`。两阶段捕获的模型请求均必须路由到 loopback Responses mock，并由 HTTP 418 在模型响应或推理前停止；原始 token/header 不得保留或记录。Doctor 必须分别通过 required checks `codex-wire` 与 `codex-file-auth-wire`；若 managed/system 配置重新注入 MCP、hook、tool 或 agent hint，必须保持 `ready: false`。密钥不得进入聊天、仓库、日志或 setup record。

持续处理，只有在当前平台入口实时返回 ready: true 后才算安装完成：macOS 使用 `./video-replacer setup status --json`，Windows 使用 `.\video-replacer.cmd setup status --json`。Windows setup identity 必须记录在线证明所得的 `official_sha256` 与 `official_release_tag`，每次 live status 必须重新读取 exact-version 官方 manifest 并重算实际 binary；不能只接受 setup record 中的旧值。两阶段捕获的模型请求必须都路由到本机 loopback mock，并由 HTTP 418 在模型响应或推理前停止；不要把这项 wire 证据解释为对进程其他网络活动的证明。安装期间不要上传任何媒体、创建远端生成任务、消费积分或启动常驻服务。最后只告诉我 READY 状态、已配置的后端和仍需我知道的安全边界。
```

仓库已经在本机时，把第一行改成：

```text
请立即把当前 Video Replacer 仓库完整配置到可用状态，并继续遵守下面全部安装要求。
```

下载文件本身不会绕过操作系统而自动执行。这里的“下载即配置好”指同一个 Codex 安装任务在 clone 后不停止，持续工作到实时 `READY`；用户不承担中间命令和配置工作。
