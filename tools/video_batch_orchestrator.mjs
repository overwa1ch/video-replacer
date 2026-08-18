#!/usr/bin/env node

/**
 * JavaScript control plane for the folder-driven video replacement batch loop.
 *
 * The Python engine owns batch state, external 0700 node-run workspaces,
 * zero-write model workspaces,
 * artifact promotion, and deterministic gates. This process owns scheduling,
 * explicit paid-command authorization, process exclusivity, health snapshots, signal
 * handling, and failure backoff.
 */

import { constants as fsConstants } from "node:fs";
import {
  access,
  chmod,
  mkdir,
  open,
  readFile,
  readdir,
  rename,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const STATE_NAMES = [
  "inbox",
  "needs-input",
  "ready",
  "running",
  "review",
  "blocked",
  "completed",
  "logs",
];

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SCRIPT_ROOT = dirname(SCRIPT_PATH);
const DEFAULT_PROJECT_ROOT = resolve(SCRIPT_ROOT, "..");
const DEFAULT_ENGINE = join(SCRIPT_ROOT, "video_batch_loop.py");
const DEFAULT_LOOP_ROOT = join(DEFAULT_PROJECT_ROOT, "workspace", "video-loop");
const DEFAULT_PYTHON = process.platform === "win32"
  ? join(DEFAULT_PROJECT_ROOT, ".venv", "Scripts", "python.exe")
  : join(DEFAULT_PROJECT_ROOT, ".venv", "bin", "python3");
const SNAPSHOT_NAME = "js-orchestrator-state.json";
const LOCK_NAME = ".js-orchestrator.lock";
const PAID_COMMANDS = new Set(["submit-job", "submit-prepared", "retry-sequential"]);
const PAYMENT_TOKEN_COMMANDS = new Set([
  "submit-job",
  "submit-prepared",
  "retry-sequential",
]);
const PREPARATION_QUEUE_COMMANDS = new Set(["prepare", "submit-prepared"]);
export const PAYMENT_AUTH_TOKEN_ENV = "VIDEO_LOOP_PAYMENT_AUTH_TOKEN";
export const PAYMENT_CHECKPOINT_TTL_SECONDS = 7200;
const DIRECT_COMMANDS = new Set([
  "init",
  "once",
  "check",
  "prepare",
  "retry-prepare",
  "submit-job",
  "submit-prepared",
  "retry-sequential",
]);

export class OrchestratorError extends Error {
  constructor(message, exitCode = 2) {
    super(message);
    this.name = "OrchestratorError";
    this.exitCode = exitCode;
  }
}

function utcNow() {
  return new Date().toISOString();
}

function parseInteger(value, label, minimum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum) {
    throw new OrchestratorError(`${label} 必须是大于或等于 ${minimum} 的整数`);
  }
  return parsed;
}

function parseNumber(value, label, minimum) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum) {
    throw new OrchestratorError(`${label} 必须是大于或等于 ${minimum} 的数字`);
  }
  return parsed;
}

function takeOption(argv, index, name) {
  const value = argv[index + 1];
  if (value === undefined || value.startsWith("--")) {
    throw new OrchestratorError(`${name} 缺少值`);
  }
  return value;
}

function parseRawArgs(argv) {
  const values = {};
  const positionals = [];
  const booleanFlags = new Set([
    "--confirm-paid",
    "--json",
    "--help",
  ]);
  const valueFlags = new Map([
    ["--config", "configPath"],
    ["--root", "loopRoot"],
    ["--project-root", "projectRoot"],
    ["--python", "python"],
    ["--engine", "engine"],
    ["--max-batch-videos", "maxBatchVideos"],
    ["--min-free-gib", "minFreeGib"],
    ["--daily-paid-limit", "dailyPaidLimit"],
    ["--interval", "interval"],
    ["--stable-seconds", "stableSeconds"],
    ["--max-consecutive-failures", "maxConsecutiveFailures"],
    ["--max-cycles", "maxCycles"],
    ["--preparation-concurrency", "preparationConcurrency"],
    ["--generation-concurrency", "generationConcurrency"],
    ["--retry-authorization-manifest", "retryAuthorizationManifest"],
    ["--task-timeout", "taskTimeout"],
  ]);

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (booleanFlags.has(token)) {
      values[token.slice(2).replaceAll("-", "_")] = true;
      continue;
    }
    if (valueFlags.has(token)) {
      values[valueFlags.get(token)] = takeOption(argv, index, token);
      index += 1;
      continue;
    }
    if (token.startsWith("--")) {
      throw new OrchestratorError(`未知参数：${token}`);
    }
    positionals.push(token);
  }
  return { values, positionals };
}

async function loadConfig(configPath) {
  if (!configPath) return {};
  const absolutePath = resolve(configPath);
  let value;
  try {
    value = JSON.parse(await readFile(absolutePath, "utf8"));
  } catch (error) {
    throw new OrchestratorError(`无法读取配置 ${absolutePath}：${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new OrchestratorError("编排器配置必须是 JSON object");
  }
  if (value.schema_version !== 1) {
    throw new OrchestratorError("编排器配置 schema_version 必须为 1");
  }
  return value;
}

function resolveConfiguredPath(value, configPath, fallback) {
  const candidate = value || fallback;
  if (isAbsolute(candidate)) return resolve(candidate);
  const base = configPath ? dirname(resolve(configPath)) : process.cwd();
  return resolve(base, candidate);
}

export async function resolveOptions(argv, environment = process.env) {
  const { values, positionals } = parseRawArgs(argv);
  const config = await loadConfig(values.configPath);
  const projectRoot = resolveConfiguredPath(
    values.projectRoot || environment.VIDEO_LOOP_PROJECT_ROOT || config.project_root,
    values.configPath,
    DEFAULT_PROJECT_ROOT,
  );
  const loopRoot = resolveConfiguredPath(
    values.loopRoot || environment.VIDEO_LOOP_ROOT || config.loop_root,
    values.configPath,
    projectRoot === DEFAULT_PROJECT_ROOT
      ? DEFAULT_LOOP_ROOT
      : join(projectRoot, "workspace", "video-loop"),
  );
  const engine = resolveConfiguredPath(
    values.engine || environment.VIDEO_LOOP_ENGINE || config.engine?.script,
    values.configPath,
    DEFAULT_ENGINE,
  );
  const python = resolveConfiguredPath(
    values.python ||
      environment.VIDEO_REPLACER_PYTHON ||
      config.engine?.python,
    values.configPath,
    DEFAULT_PYTHON,
  );

  const command = positionals[0] || (values.help ? "help" : undefined);
  const batch = positionals[1];
  const jobId = positionals[2];
  const maxPositionals = ["submit-job", "retry-prepare"].includes(command) ? 3 : 2;
  if (positionals.length > maxPositionals) {
    throw new OrchestratorError(`多余的位置参数：${positionals.slice(2).join(" ")}`);
  }

  const limits = config.limits || {};
  const watch = config.watch || {};
  const requestedPreparationConcurrency = parseInteger(
    values.preparationConcurrency ??
      environment.VIDEO_LOOP_PREPARATION_CONCURRENCY ??
      config.concurrency?.preparation ??
      1,
    "preparation-concurrency",
    1,
  );
  const options = {
    command,
    batch,
    jobId,
    help: Boolean(values.help),
    json: Boolean(values.json),
    execute: false,
    autoReady: false,
    confirmPaid: Boolean(values.confirm_paid),
    paymentAuthorizationToken: values.confirm_paid
      ? randomBytes(32).toString("hex")
      : null,
    configPath: values.configPath ? resolve(values.configPath) : null,
    projectRoot,
    loopRoot,
    engine,
    python,
    maxBatchVideos: parseInteger(
      values.maxBatchVideos ??
        environment.VIDEO_LOOP_MAX_BATCH_VIDEOS ??
        limits.max_batch_videos ??
        15,
      "max-batch-videos",
      1,
    ),
    minFreeGib: parseNumber(
      values.minFreeGib ??
        environment.VIDEO_LOOP_MIN_FREE_GIB ??
        limits.min_free_gib ??
        10,
      "min-free-gib",
      0,
    ),
    dailyPaidLimit: parseInteger(
      values.dailyPaidLimit ??
        environment.VIDEO_LOOP_DAILY_PAID_LIMIT ??
        limits.daily_paid_limit ??
        1,
      "daily-paid-limit",
      1,
    ),
    interval: parseNumber(
      values.interval ?? watch.interval_seconds ?? 5,
      "interval",
      1,
    ),
    stableSeconds: parseNumber(
      values.stableSeconds ?? watch.stable_seconds ?? 10,
      "stable-seconds",
      0,
    ),
    maxConsecutiveFailures: parseInteger(
      values.maxConsecutiveFailures ?? watch.max_consecutive_failures ?? 5,
      "max-consecutive-failures",
      1,
    ),
    maxCycles: parseInteger(values.maxCycles ?? 0, "max-cycles", 0),
    // The effective policy is always strict FIFO.  Keep accepting legacy
    // env/config values so read-only commands and older installations do not
    // fail before they can report status.  A misleading explicit CLI override
    // is rejected below only for commands that actually run the FIFO queue.
    preparationConcurrency: 1,
    requestedPreparationConcurrency,
    preparationConcurrencyCliOverride:
      values.preparationConcurrency === undefined
        ? null
        : requestedPreparationConcurrency,
    generationConcurrency: parseInteger(
      values.generationConcurrency ??
        environment.VIDEO_LOOP_GENERATION_CONCURRENCY ??
        config.concurrency?.generation ??
        2,
      "generation-concurrency",
      1,
    ),
    retryAuthorizationManifest: values.retryAuthorizationManifest
      ? resolve(values.retryAuthorizationManifest)
      : null,
    taskTimeout: parseInteger(values.taskTimeout ?? 86400, "task-timeout", 1),
  };
  validateOptions(options);
  return options;
}

export function validateOptions(options) {
  if (!options.command) {
    throw new OrchestratorError("缺少命令；使用 --help 查看入口");
  }
  const known = new Set([...DIRECT_COMMANDS, "watch", "status", "help"]);
  if (!known.has(options.command)) {
    throw new OrchestratorError(`未知命令：${options.command}`);
  }
  if (["check", "prepare", "submit-job", "submit-prepared", "retry-sequential"].includes(options.command) && !options.batch) {
    throw new OrchestratorError(`${options.command} 需要 batch 名称或路径`);
  }
  if (options.command === "submit-job" && !options.jobId) {
    throw new OrchestratorError("submit-job 需要 V 编号");
  }
  if (options.command === "retry-prepare" && (!options.batch || !options.jobId)) {
    throw new OrchestratorError("retry-prepare 需要 batch 和 V 编号");
  }
  if (
    PREPARATION_QUEUE_COMMANDS.has(options.command) &&
    options.preparationConcurrencyCliOverride !== null &&
    options.preparationConcurrencyCliOverride !== 1
  ) {
    throw new OrchestratorError(
      "preparation-concurrency 当前必须为 1；本地提示词准备采用严格 FIFO",
    );
  }
  if (PAID_COMMANDS.has(options.command) && !options.confirmPaid) {
    throw new OrchestratorError(
      "付费执行未解锁：本次启动必须同时提供 --confirm-paid",
    );
  }
  if (options.confirmPaid && !PAID_COMMANDS.has(options.command)) {
    throw new OrchestratorError("--confirm-paid 只适用于显式提交或恢复命令");
  }
  if (options.command === "retry-sequential" && !options.retryAuthorizationManifest) {
    throw new OrchestratorError(
      "retry-sequential 需要 --retry-authorization-manifest",
    );
  }
}

export function buildEngineArgs(options, command = options.command) {
  const args = [
    options.engine,
    "--root",
    options.loopRoot,
    "--project-root",
    options.projectRoot,
    "--max-batch-videos",
    String(options.maxBatchVideos),
    "--min-free-gib",
    String(options.minFreeGib),
    "--daily-paid-limit",
    String(options.dailyPaidLimit),
    command,
  ];
  if (command === "once") {
    args.push("--stable-seconds", String(options.stableSeconds));
  } else if (["check", "prepare", "submit-prepared", "retry-sequential"].includes(command)) {
    args.push(options.batch);
    if (command === "retry-sequential") {
      args.push(
        "--retry-authorization-manifest",
        options.retryAuthorizationManifest,
        "--task-timeout",
        String(options.taskTimeout),
      );
    }
  } else if (["inspect-flow", "preflight-submission"].includes(command)) {
    args.push(options.batch);
    if (command === "inspect-flow" && options.preparationOnly) {
      args.push("--preparation-only");
    }
  } else if (["prepare-job", "retry-prepare-job", "submit-job"].includes(command)) {
    args.push(options.batch, options.jobId);
    if (command === "submit-job") {
      args.push("--task-timeout", String(options.taskTimeout));
    }
  } else if (["finalize-preparation-flow", "finalize-flow"].includes(command)) {
    args.push(options.batch);
  }
  return args;
}

async function requireFile(path, label) {
  try {
    const metadata = await stat(path);
    if (!metadata.isFile()) throw new Error("not a file");
    await access(path, fsConstants.R_OK);
  } catch (error) {
    throw new OrchestratorError(`${label}不可用：${path} (${error.message})`);
  }
}

async function ensureRuntime(options) {
  await requireFile(
    options.python,
    "仓库 Python 3.12 runtime；请先运行 python3.12 tools/bootstrap.py",
  );
  await requireFile(options.engine, "Python 批次引擎");
  await mkdir(join(options.loopRoot, "logs"), { recursive: true, mode: 0o700 });
}

function tailText(value, maxLength = 8000) {
  return value.length <= maxLength ? value : value.slice(-maxLength);
}

export async function runEngine(options, command = options.command, io = {}) {
  await ensureRuntime(options);
  const args = buildEngineArgs(options, command);
  const childEnvironment = { ...process.env };
  delete childEnvironment[PAYMENT_AUTH_TOKEN_ENV];
  if (PAYMENT_TOKEN_COMMANDS.has(command)) {
    if (
      !options.confirmPaid ||
      typeof options.paymentAuthorizationToken !== "string" ||
      !/^[0-9a-f]{64}$/.test(options.paymentAuthorizationToken)
    ) {
      throw new OrchestratorError(
        `${command} 缺少当前 JavaScript 进程付费 capability`,
      );
    }
    childEnvironment[PAYMENT_AUTH_TOKEN_ENV] = options.paymentAuthorizationToken;
  }
  const child = spawn(options.python, args, {
    cwd: options.projectRoot,
    env: childEnvironment,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    if (io.stream !== false) process.stdout.write(chunk);
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
    if (io.stream !== false) process.stderr.write(chunk);
  });
  const result = await new Promise((resolvePromise, rejectPromise) => {
    child.once("error", rejectPromise);
    child.once("close", (code, signal) =>
      resolvePromise({ code: code ?? 1, signal, stdout, stderr }),
    );
  });
  if (result.code !== 0 && !io.allowFailure) {
    throw new OrchestratorError(
      `Python 引擎退出 ${result.code}：${tailText(stderr || stdout, 2000).trim()}`,
      result.code,
    );
  }
  return result;
}

function parseWorkerJson(result, command) {
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    throw new OrchestratorError(
      `${command} worker 没有返回有效 JSON：${tailText(result.stdout || result.stderr, 1000)}`,
    );
  }
}

async function runWorkerJson(options, command) {
  const result = await runEngine(options, command, { stream: false });
  return parseWorkerJson(result, command);
}

export class Semaphore {
  constructor(limit) {
    if (!Number.isInteger(limit) || limit < 1) {
      throw new OrchestratorError("semaphore limit 必须大于 0");
    }
    this.limit = limit;
    this.active = 0;
    this.waiters = [];
  }

  async acquire() {
    if (this.active < this.limit) {
      this.active += 1;
      return;
    }
    await new Promise((resolvePromise) => this.waiters.push(resolvePromise));
  }

  release() {
    const next = this.waiters.shift();
    if (next) {
      next();
    } else {
      this.active -= 1;
    }
  }

  async use(callback) {
    await this.acquire();
    try {
      return await callback();
    } finally {
      this.release();
    }
  }
}

export async function runJobPipeline(jobs, controls) {
  const preparationPool = new Semaphore(controls.preparationConcurrency);
  const generationPool = new Semaphore(controls.generationConcurrency);
  const settled = await Promise.allSettled(
    jobs.map(async (job) => {
      const preparation = await preparationPool.use(() =>
        controls.prepareJob(job),
      );
      const preparedJob = preparation?.job;
      if (
        !controls.execute ||
        controls.allowSubmission === false ||
        !preparedJob ||
        preparedJob.status !== "READY_FOR_SUBMISSION"
      ) {
        return { id: job.id, preparation, submission: null };
      }
      const submission = await generationPool.use(() =>
        controls.submitJob(job, preparation),
      );
      return { id: job.id, preparation, submission };
    }),
  );
  const failure = settled.find((item) => item.status === "rejected");
  if (failure) throw failure.reason;
  return settled.map((item) => item.value);
}

export async function runPreparationQueue(jobs, controls) {
  const queuedAt = utcNow();
  const queuedAtMonotonic = process.hrtime.bigint();
  const schedulePath = join(
    controls.batch,
    "streaming-results",
    controls.scheduleName || "preparation-schedule.json",
  );
  const schedule = {
    schema_version: 1,
    batch_id: basename(controls.batch),
    policy: "STRICT_FIFO",
    effective_concurrency: 1,
    created_at: queuedAt,
    updated_at: queuedAt,
    jobs: jobs.map((job, index) => ({
      id: job.id,
      queue_index: index + 1,
      phase: "QUEUED",
      queued_at: queuedAt,
      worker_started_at: null,
      worker_finished_at: null,
      queue_wait_seconds: null,
      worker_seconds: null,
    })),
  };
  await atomicWriteJson(schedulePath, schedule);
  const results = [];
  for (let index = 0; index < jobs.length; index += 1) {
    const job = jobs[index];
    const record = schedule.jobs[index];
    const workerStartedMonotonic = process.hrtime.bigint();
    record.phase = "WORKER_RUNNING";
    record.worker_started_at = utcNow();
    record.queue_wait_seconds = Number(
      (Number(workerStartedMonotonic - queuedAtMonotonic) / 1e9).toFixed(6),
    );
    schedule.updated_at = utcNow();
    await atomicWriteJson(schedulePath, schedule);
    try {
      results.push(await controls.prepareJob(job));
      record.phase = "FINISHED";
    } catch (error) {
      record.phase = "FAILED";
      record.error = tailText(String(error?.message || error), 1000);
      throw error;
    } finally {
      const workerFinishedMonotonic = process.hrtime.bigint();
      record.worker_finished_at = utcNow();
      record.worker_seconds = Number(
        (Number(workerFinishedMonotonic - workerStartedMonotonic) / 1e9).toFixed(6),
      );
      schedule.updated_at = utcNow();
      await atomicWriteJson(schedulePath, schedule);
    }
  }
  return results;
}

function paymentCheckpointAuthorizer(options, batch, flow) {
  let profileManagedPromise = null;
  let sharedLegacyCheckpoint = null;
  return async (jobId) => {
    profileManagedPromise ??= batchUsesBackendProfile(batch);
    if (await profileManagedPromise) {
      // Profile-managed approvals bind each Job's final media and therefore
      // use separate checkpoint paths.
      return writePaymentCheckpoint(options, batch, flow, { jobIds: [jobId] });
    }
    // Frozen legacy flows use one batch-wide approval.  Every concurrent Job
    // must await the same atomic write: rewriting that shared file while an
    // earlier worker is reading it is rejected by Windows file sharing and is
    // unnecessary because the capability, PID, flow and scope are identical.
    sharedLegacyCheckpoint ??= writePaymentCheckpoint(options, batch, flow);
    return sharedLegacyCheckpoint;
  };
}

export function paymentAuthorizationBinding(token, payload) {
  const values = [
    "video-loop-payment-v1",
    payload.batch_id,
    payload.flow_fingerprint,
    String(payload.planned_paid_tasks),
    payload.authorization_scope,
    String(payload.orchestrator_pid),
    payload.authorization_nonce,
    payload.authorized_at,
    payload.expires_at,
  ];
  if (payload.schema_version === 3) {
    if (!Array.isArray(payload.submission_identities)) {
      throw new OrchestratorError(
        "schema v3 payment-checkpoint 缺少 submission_identities",
      );
    }
    values.push(canonicalJsonSha256(payload.submission_identities));
  }
  values.push(token);
  return createHash("sha256").update(values.join("\n"), "utf8").digest("hex");
}

function canonicalizeJson(value) {
  if (Array.isArray(value)) return value.map(canonicalizeJson);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalizeJson(value[key])]),
    );
  }
  return value;
}

function canonicalJsonSha256(value) {
  return createHash("sha256")
    .update(JSON.stringify(canonicalizeJson(value)), "utf8")
    .digest("hex");
}

async function sha256File(path) {
  let handle;
  try {
    handle = await open(path, "r");
    const digest = createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    while (true) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      digest.update(buffer.subarray(0, bytesRead));
    }
    return digest.digest("hex");
  } finally {
    if (handle) await handle.close();
  }
}

function isSha256(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

async function readJsonObject(path, label) {
  let value;
  try {
    value = JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    throw new OrchestratorError(`${label} 无法读取：${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new OrchestratorError(`${label} 必须是 JSON object`);
  }
  return value;
}

function pathWithin(root, candidate, label) {
  const rootPath = resolve(root);
  const resolved = resolve(rootPath, String(candidate));
  const relation = relative(rootPath, resolved);
  if (relation === "" || (!relation.startsWith("..") && !isAbsolute(relation))) {
    return resolved;
  }
  throw new OrchestratorError(`${label} 不在 Job 输出目录内`);
}

async function batchUsesBackendProfile(batch) {
  const bindingsPath = join(batch, "job-bindings.json");
  try {
    const bindings = await readJsonObject(bindingsPath, "job-bindings.json");
    const managed = bindings.schema_version === 3 && typeof bindings.backend_profile === "string";
    const activeProfile = String(process.env.VIDEO_REPLACER_ACTIVE_PROFILE || "").trim();
    if (managed && activeProfile && bindings.backend_profile !== activeProfile) {
      throw new OrchestratorError(
        "job-bindings.json 的 backend_profile 与当前 Agent 验证的 setup profile 不一致",
      );
    }
    return managed;
  } catch (error) {
    if (error instanceof OrchestratorError && /ENOENT/.test(error.message)) {
      return false;
    }
    throw error;
  }
}

async function submissionAuthorizationIdentity(options, batch, jobId) {
  const outputDir = resolve(
    options.projectRoot,
    "outputs",
    "video-replacements",
    `${basename(batch)}-${jobId}`,
  );
  const planPath = join(outputDir, "submission-plan.json");
  let plan;
  try {
    plan = await readJsonObject(planPath, `${jobId} submission-plan.json`);
  } catch (error) {
    if (error instanceof OrchestratorError && /ENOENT/.test(error.message)) {
      return null;
    }
    throw error;
  }
  if (plan.schema_version !== 2) return null;
  if (
    plan.job_id !== jobId ||
    typeof plan.backend_profile !== "string" ||
    !isSha256(plan.backend_profile_constraints_sha256) ||
    !isSha256(plan.preflight_manifest_sha256) ||
    typeof plan.preflight_manifest !== "string" ||
    typeof plan.upload_preparation_manifest !== "string"
  ) {
    throw new OrchestratorError(`${jobId} schema v3 提交计划字段无效`);
  }
  const preflightPath = pathWithin(
    outputDir,
    plan.preflight_manifest,
    `${jobId} preflight manifest`,
  );
  const preflightSha = await sha256File(preflightPath);
  if (preflightSha !== plan.preflight_manifest_sha256) {
    throw new OrchestratorError(`${jobId} preflight manifest 已变化`);
  }
  const preflight = await readJsonObject(preflightPath, `${jobId} preflight manifest`);
  const uploadPath = pathWithin(
    outputDir,
    plan.upload_preparation_manifest,
    `${jobId} upload-preparation manifest`,
  );
  const uploadSha = await sha256File(uploadPath);
  const active = preflight.active_video;
  if (!active || typeof active !== "object" || Array.isArray(active)) {
    throw new OrchestratorError(`${jobId} preflight 缺少最终视频`);
  }
  const finalVideoPath = pathWithin(
    outputDir,
    active.path,
    `${jobId} 最终视频`,
  );
  const finalVideoSha = await sha256File(finalVideoPath);
  if (
    preflight.backend_profile !== plan.backend_profile ||
    preflight.constraints_digest !== plan.backend_profile_constraints_sha256 ||
    typeof preflight.model_version !== "string" ||
    !preflight.model_version ||
    preflight.upload_preparation_manifest !== uploadPath ||
    preflight.upload_preparation_manifest_sha256 !== uploadSha ||
    active.path !== finalVideoPath ||
    active.sha256 !== finalVideoSha
  ) {
    throw new OrchestratorError(
      `${jobId} 的最终视频、上传准备 manifest 或 backend profile 已变化`,
    );
  }
  return {
    job_id: jobId,
    backend_profile: plan.backend_profile,
    backend_profile_constraints_sha256: plan.backend_profile_constraints_sha256,
    model_version_sha256: createHash("sha256")
      .update(preflight.model_version, "utf8")
      .digest("hex"),
    final_video_sha256: finalVideoSha,
    preflight_manifest_sha256: preflightSha,
    upload_preparation_manifest_sha256: uploadSha,
  };
}

async function submissionAuthorizationIdentities(options, batch, jobIds) {
  const profileManaged = await batchUsesBackendProfile(batch);
  const identities = await Promise.all(
    jobIds.map((jobId) => submissionAuthorizationIdentity(options, batch, jobId)),
  );
  if (!profileManaged) {
    if (identities.some((identity) => identity !== null)) {
      throw new OrchestratorError("旧版批次不能混用 schema v3 提交计划");
    }
    return null;
  }
  if (identities.some((identity) => identity === null)) {
    throw new OrchestratorError("schema v3 批次缺少已验证的提交计划");
  }
  return identities.sort((left, right) => left.job_id.localeCompare(right.job_id));
}

async function writePaymentCheckpoint(options, batch, flow, { jobIds = null } = {}) {
  if (
    !options.confirmPaid ||
    typeof options.paymentAuthorizationToken !== "string" ||
    !/^[0-9a-f]{64}$/.test(options.paymentAuthorizationToken)
  ) {
    throw new OrchestratorError(
      "无法创建 payment-checkpoint：缺少当前 JavaScript 进程付费 capability",
    );
  }
  const jobs = Array.isArray(flow.jobs) ? flow.jobs : [];
  const flowJobIds = jobs
    .filter((job) => job?.skipped !== true)
    .map((job) => String(job?.id || ""));
  const requestedJobIds = jobIds === null ? flowJobIds : jobIds;
  if (
    !Array.isArray(requestedJobIds) ||
    requestedJobIds.length < 1 ||
    new Set(requestedJobIds).size !== requestedJobIds.length ||
    requestedJobIds.some((jobId) => !flowJobIds.includes(jobId))
  ) {
    throw new OrchestratorError("payment-checkpoint 的指定 Job 不在当前 flow 中");
  }
  const submissionIdentities = await submissionAuthorizationIdentities(
    options,
    batch,
    requestedJobIds,
  );
  // Legacy checkpoints retain their batch-wide v2 contract.  A profile-managed
  // v3 approval is intentionally narrowed to one ready Job so parallel workers
  // never overwrite each other's final-media authorization.
  const authorizedJobIds = submissionIdentities === null ? flowJobIds : requestedJobIds;
  const singleJobScope = authorizedJobIds.length !== flowJobIds.length;
  const checkpointPath =
    submissionIdentities !== null && singleJobScope
      ? join(batch, `payment-checkpoint-${authorizedJobIds[0]}.json`)
      : join(batch, "payment-checkpoint.json");
  const authorizedAt = utcNow();
  const payload = {
    schema_version: submissionIdentities === null ? 2 : 3,
    batch_id: basename(batch),
    flow_fingerprint: flow.flow_fingerprint,
    planned_paid_tasks: authorizedJobIds.length,
    explicit_payment_approval_received: true,
    authorization_scope: singleJobScope
      ? "current-batch-single-job"
      : "current-batch",
    orchestrator_pid: process.pid,
    authorization_nonce: randomBytes(16).toString("hex"),
    authorized_at: authorizedAt,
    expires_at: new Date(
      Date.parse(authorizedAt) + PAYMENT_CHECKPOINT_TTL_SECONDS * 1000,
    ).toISOString(),
  };
  if (singleJobScope) payload.authorized_job_ids = authorizedJobIds;
  if (submissionIdentities !== null) {
    payload.submission_identities = submissionIdentities;
  }
  payload.authorization_binding_sha256 = paymentAuthorizationBinding(
    options.paymentAuthorizationToken,
    payload,
  );
  try {
    const existing = JSON.parse(await readFile(checkpointPath, "utf8"));
    if (
      existing.flow_fingerprint !== payload.flow_fingerprint ||
      existing.batch_id !== payload.batch_id ||
      existing.explicit_payment_approval_received !== true
    ) {
      throw new OrchestratorError(
        `既有 payment-checkpoint 与当前 flow 不匹配：${checkpointPath}`,
      );
    }
    await atomicWriteJson(checkpointPath, payload);
    return payload;
  } catch (error) {
    if (error instanceof OrchestratorError) throw error;
    if (error.code !== "ENOENT") {
      throw new OrchestratorError(`无法读取 payment-checkpoint：${error.message}`);
    }
  }
  await atomicWriteJson(checkpointPath, payload);
  return payload;
}

async function readStoredStreamingFlow(batch) {
  const flowPath = join(batch, "streaming-flow.json");
  let flow;
  try {
    flow = JSON.parse(await readFile(flowPath, "utf8"));
  } catch (error) {
    throw new OrchestratorError(
      `无法读取 retry-sequential 的 streaming flow：${error.message}`,
    );
  }
  if (
    !flow ||
    typeof flow !== "object" ||
    flow.batch_id !== basename(batch) ||
    typeof flow.flow_fingerprint !== "string" ||
    !Array.isArray(flow.jobs)
  ) {
    throw new OrchestratorError(
      "retry-sequential 的 streaming-flow.json 与当前批次不匹配",
    );
  }
  return flow;
}

async function resolveBatchPath(loopRoot, value) {
  const direct = resolve(value);
  try {
    if ((await stat(direct)).isDirectory()) return direct;
  } catch {
    // Fall through to state-directory lookup.
  }
  for (const stateName of STATE_NAMES) {
    const candidate = join(loopRoot, stateName, value);
    try {
      if ((await stat(candidate)).isDirectory()) return candidate;
    } catch {
      // Continue searching.
    }
  }
  throw new OrchestratorError(`找不到批次：${value}`);
}

export async function runStreamingBatch(options, batch, { preparationOnly = false } = {}) {
  const scoped = { ...options, batch, preparationOnly };
  const flow = await runWorkerJson(scoped, "inspect-flow");
  const jobs = Array.isArray(flow.jobs) ? flow.jobs : [];
  const preparations = await runPreparationQueue(jobs, {
    batch,
    prepareJob: (job) =>
      runWorkerJson({ ...scoped, jobId: job.id }, "prepare-job"),
  });
  const preparationByJob = new Map(
    preparations.map((preparation, index) => [jobs[index].id, preparation]),
  );
  const authorizeJob = paymentCheckpointAuthorizer(scoped, batch, flow);
  const results = await runJobPipeline(jobs, {
    preparationConcurrency: 1,
    generationConcurrency: options.generationConcurrency,
    execute: options.execute,
    allowSubmission: !preparationOnly,
    prepareJob: (job) => preparationByJob.get(job.id),
    submitJob: async (job) => {
      await authorizeJob(job.id);
      return runWorkerJson({ ...scoped, jobId: job.id }, "submit-job");
    },
  });
  const finalizeCommand = preparationOnly
    ? "finalize-preparation-flow"
    : "finalize-flow";
  const finalized = await runWorkerJson(scoped, finalizeCommand);
  return { flow, results, finalized };
}

export async function runPreparedStreamingBatch(options, batch) {
  const scoped = {
    ...options,
    batch,
    execute: true,
    preparationOnly: true,
  };
  const flow = await runWorkerJson(scoped, "inspect-flow");
  const jobs = Array.isArray(flow.jobs) ? flow.jobs : [];
  const preparations = await runPreparationQueue(jobs, {
    batch,
    scheduleName: "submission-revalidation-schedule.json",
    prepareJob: (job) =>
      runWorkerJson({ ...scoped, jobId: job.id }, "prepare-job"),
  });
  const preparationByJob = new Map(
    preparations.map((preparation, index) => [jobs[index].id, preparation]),
  );
  const localPreparation = await runWorkerJson(
    scoped,
    "finalize-preparation-flow",
  );
  await runWorkerJson(scoped, "preflight-submission");
  const authorizeJob = paymentCheckpointAuthorizer(scoped, batch, flow);
  const results = await runJobPipeline(jobs, {
    preparationConcurrency: 1,
    generationConcurrency: options.generationConcurrency,
    execute: true,
    allowSubmission: true,
    prepareJob: (job) => preparationByJob.get(job.id),
    submitJob: async (job) => {
      await authorizeJob(job.id);
      return runWorkerJson({ ...scoped, jobId: job.id }, "submit-job");
    },
  });
  const finalized = await runWorkerJson(scoped, "finalize-flow");
  return { flow, preparations: localPreparation, results, finalized };
}

export async function runPreparedStreamingJob(options, batch, jobId) {
  const scoped = {
    ...options,
    batch,
    jobId,
    execute: true,
    preparationOnly: true,
  };
  const flow = await runWorkerJson(scoped, "inspect-flow");
  const jobs = Array.isArray(flow.jobs) ? flow.jobs : [];
  if (!jobs.some((job) => job?.id === jobId && job?.skipped !== true)) {
    throw new OrchestratorError(`当前 flow 中没有可提交的 ${jobId}`);
  }
  await runWorkerJson({ ...scoped, jobId }, "prepare-job");
  await runWorkerJson(scoped, "finalize-preparation-flow");
  await runWorkerJson(scoped, "preflight-submission");
  await writePaymentCheckpoint(scoped, batch, flow, { jobIds: [jobId] });
  const submission = await runWorkerJson(scoped, "submit-job");
  return { flow, submission };
}

async function visibleEntries(path) {
  try {
    const values = await readdir(path, { withFileTypes: true });
    return values
      .filter((entry) => !entry.name.startsWith("."))
      .map((entry) => ({
        name: entry.name,
        kind: entry.isDirectory() ? "directory" : "file",
      }));
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
}

export async function readLoopSummary(loopRoot) {
  const states = {};
  for (const name of STATE_NAMES) {
    if (name === "logs") continue;
    const entries = await visibleEntries(join(loopRoot, name));
    states[name] = {
      count: entries.length,
      entries,
    };
  }
  let orchestrator = null;
  try {
    orchestrator = JSON.parse(
      await readFile(join(loopRoot, "logs", SNAPSHOT_NAME), "utf8"),
    );
  } catch (error) {
    if (error.code !== "ENOENT") {
      orchestrator = { status: "unreadable", error: error.message };
    }
  }
  return {
    schema_version: 1,
    loop_root: loopRoot,
    observed_at: utcNow(),
    states,
    orchestrator,
  };
}

async function readOptionalJson(path) {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw new OrchestratorError(`无法读取状态文件 ${path}：${error.message}`);
  }
}

export async function readBatchStatus(batch) {
  const loopState = await readOptionalJson(join(batch, "loop-state.json"));
  const flow = await readOptionalJson(join(batch, "streaming-flow.json"));
  const jobs = Array.isArray(flow?.jobs) ? flow.jobs : [];
  const preparationSchedule = await readOptionalJson(
    join(batch, "streaming-results", "preparation-schedule.json"),
  );
  const submissionRevalidationSchedule = await readOptionalJson(
    join(batch, "streaming-results", "submission-revalidation-schedule.json"),
  );
  const latestNodeProcesses = await readLatestNodeProcesses(batch);
  const preparation = [];
  for (const rawJob of jobs) {
    const jobId = rawJob?.id;
    if (typeof jobId !== "string") continue;
    const result = await readOptionalJson(
      join(batch, "streaming-results", "preparation", `${jobId}.json`),
    );
    preparation.push({
      id: jobId,
      skipped: rawJob?.skipped === true,
      preparation_status: result?.job?.status || "NOT_PREPARED",
      blocker: result?.job?.blocker || null,
    });
  }
  const loopClaimsPaidReady =
    loopState?.state === "LOCAL_PREPARED_AWAITING_APPROVAL" ||
    loopState?.payment_approval_required === true;
  const canonicalPreparationBlocked =
    loopClaimsPaidReady &&
    preparation.some(
      (job) => job.skipped !== true && job.preparation_status !== "READY_FOR_SUBMISSION",
    );
  return {
    schema_version: 1,
    batch_id: basename(batch),
    batch_path: batch,
    state: canonicalPreparationBlocked
      ? "LOCAL_PREPARATION_BLOCKED"
      : loopState?.state || "NOT_INSPECTED",
    payment_approval_required:
      !canonicalPreparationBlocked &&
      loopState?.payment_approval_required === true,
    flow_fingerprint: flow?.flow_fingerprint || null,
    schedules: {
      preparation: preparationSchedule,
      submission_revalidation: submissionRevalidationSchedule,
    },
    latest_node_process:
      latestNodeProcesses.length > 0 ? latestNodeProcesses[0] : null,
    latest_node_processes: latestNodeProcesses,
    preparation,
    observed_at: utcNow(),
  };
}

async function readLatestNodeProcesses(batch) {
  const agentsRoot = join(batch, "streaming-results", "agents");
  let scopes;
  try {
    scopes = await readdir(agentsRoot, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw new OrchestratorError(`无法读取节点运行状态 ${agentsRoot}：${error.message}`);
  }
  const records = [];
  for (const scopeEntry of scopes) {
    if (!scopeEntry.isDirectory() || scopeEntry.name.startsWith(".")) continue;
    const scopePath = join(agentsRoot, scopeEntry.name);
    let invocations;
    try {
      invocations = await readdir(scopePath, { withFileTypes: true });
    } catch (error) {
      throw new OrchestratorError(`无法读取节点运行状态 ${scopePath}：${error.message}`);
    }
    const candidates = invocations
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith("."))
      .sort((left, right) => right.name.localeCompare(left.name));
    let invocation = null;
    let processRecord = null;
    for (const candidate of candidates) {
      const candidateRecord = await readOptionalJson(
        join(scopePath, candidate.name, "codex-process.json"),
      );
      if (candidateRecord && typeof candidateRecord === "object") {
        invocation = candidate;
        processRecord = candidateRecord;
        break;
      }
    }
    if (!invocation || !processRecord) continue;
    let phase = processRecord.phase;
    let phaseSource = "recorded";
    if (typeof phase !== "string" || !phase) {
      phaseSource = "inferred_legacy";
      if (processRecord.timed_out === true) phase = "TIMED_OUT";
      else if (Number.isInteger(processRecord.returncode)) {
        phase = processRecord.returncode === 0 ? "SUCCEEDED" : "FAILED";
      } else if (processRecord.finished_at) phase = "FINISHED_UNKNOWN";
      else phase = "UNKNOWN";
    }
    records.push({
      scope: scopeEntry.name,
      invocation_id: invocation.name,
      phase,
      phase_source: phaseSource,
      stage: processRecord.stage || null,
      job_ids: Array.isArray(processRecord.job_ids) ? processRecord.job_ids : [],
      queued_at: processRecord.queued_at || null,
      started_at: processRecord.started_at || null,
      lock_acquired_at: processRecord.lock_acquired_at || null,
      exec_started_at: processRecord.exec_started_at || null,
      exec_finished_at: processRecord.exec_finished_at || null,
      auth_lock_wait_seconds: processRecord.auth_lock_wait_seconds ?? null,
      exec_seconds: processRecord.exec_seconds ?? null,
      total_seconds: processRecord.total_seconds ?? null,
      transport_reconnect_count: processRecord.transport_reconnect_count ?? null,
    });
  }
  return records.sort((left, right) => {
    const leftTime = left.queued_at || left.started_at || left.invocation_id;
    const rightTime = right.queued_at || right.started_at || right.invocation_id;
    return rightTime.localeCompare(leftTime);
  });
}

async function atomicWriteJson(path, value) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await chmod(temporary, 0o600);
  await rename(temporary, path);
  await chmod(path, 0o600);
}

async function writeSnapshot(options, value) {
  await atomicWriteJson(join(options.loopRoot, "logs", SNAPSHOT_NAME), {
    schema_version: 1,
    orchestrator: "javascript",
    pid: process.pid,
    mode: "shadow",
    project_root: options.projectRoot,
    loop_root: options.loopRoot,
    engine: {
      python: options.python,
      script: options.engine,
    },
    ...value,
    updated_at: utcNow(),
  });
}

function processExists(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

export async function acquireWatchLock(loopRoot) {
  const path = join(loopRoot, "logs", LOCK_NAME);
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const handle = await open(path, "wx", 0o600);
      await handle.writeFile(`${JSON.stringify({ pid: process.pid, started_at: utcNow() })}\n`);
      await handle.close();
      return async () => {
        try {
          await unlink(path);
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
        }
      };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let owner = null;
      try {
        owner = JSON.parse(await readFile(path, "utf8"));
      } catch {
        // An unreadable lock is treated as stale only after process ownership
        // cannot be established.
      }
      if (owner && processExists(Number(owner.pid))) {
        throw new OrchestratorError(
          `另一个 JavaScript 编排器正在运行（PID ${owner.pid}）`,
        );
      }
      await unlink(path);
    }
  }
  throw new OrchestratorError("无法取得 JavaScript 编排器锁");
}

async function withOrchestratorLock(options, callback) {
  const release = await acquireWatchLock(options.loopRoot);
  try {
    await runWorkerJson(options, "cleanup-node-runs");
    return await callback();
  } finally {
    await release();
  }
}

function delay(milliseconds, signal) {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolvePromise) => {
    const timer = setTimeout(resolvePromise, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolvePromise();
      },
      { once: true },
    );
  });
}

export async function runWatch(options, control = {}) {
  await ensureRuntime(options);
  const release = await acquireWatchLock(options.loopRoot);
  const abortController = control.abortController || new AbortController();
  const signal = abortController.signal;
  const startedAt = utcNow();
  let cycle = 0;
  let consecutiveFailures = 0;
  let exitCode = 0;
  const stop = () => abortController.abort();
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  await writeSnapshot(options, {
    command: "watch",
    status: "starting",
    started_at: startedAt,
    cycle,
    consecutive_failures: 0,
  });

  try {
    await runWorkerJson(options, "cleanup-node-runs");
    while (!signal.aborted && (options.maxCycles === 0 || cycle < options.maxCycles)) {
      cycle += 1;
      const runStartedAt = utcNow();
      let result;
      try {
        result = await runEngine(options, "once", {
          stream: control.stream !== false,
          allowFailure: true,
        });
      } catch (error) {
        result = {
          code: error instanceof OrchestratorError ? error.exitCode : 1,
          signal: null,
          stdout: "",
          stderr: error.message,
        };
      }
      if (result.code === 0) {
        consecutiveFailures = 0;
      } else {
        consecutiveFailures += 1;
        exitCode = result.code;
      }
      await writeSnapshot(options, {
        command: "watch",
        status: result.code === 0 ? "healthy" : "degraded",
        started_at: startedAt,
        cycle,
        consecutive_failures: consecutiveFailures,
        last_run: {
          started_at: runStartedAt,
          finished_at: utcNow(),
          exit_code: result.code,
          signal: result.signal,
          stdout_tail: tailText(result.stdout),
          stderr_tail: tailText(result.stderr),
        },
      });
      if (consecutiveFailures >= options.maxConsecutiveFailures) {
        throw new OrchestratorError(
          `连续 ${consecutiveFailures} 次扫描失败，编排器已 fail-closed 停止`,
          exitCode || 1,
        );
      }
      if (!signal.aborted && (options.maxCycles === 0 || cycle < options.maxCycles)) {
        const multiplier = consecutiveFailures > 0
          ? Math.min(2 ** (consecutiveFailures - 1), 12)
          : 1;
        await delay(options.interval * 1000 * multiplier, signal);
      }
    }
    await writeSnapshot(options, {
      command: "watch",
      status: signal.aborted ? "stopped" : "completed",
      started_at: startedAt,
      stopped_at: utcNow(),
      cycle,
      consecutive_failures: consecutiveFailures,
    });
    return signal.aborted ? 130 : exitCode;
  } catch (error) {
    await writeSnapshot(options, {
      command: "watch",
      status: "failed",
      started_at: startedAt,
      failed_at: utcNow(),
      cycle,
      consecutive_failures: consecutiveFailures,
      error: error.message,
    });
    throw error;
  } finally {
    process.removeListener("SIGINT", stop);
    process.removeListener("SIGTERM", stop);
    await release();
  }
}

function printHumanStatus(summary) {
  console.log(`JavaScript orchestrator: ${summary.orchestrator?.status || "not started"}`);
  for (const [state, value] of Object.entries(summary.states)) {
    const names = value.entries.map((entry) => entry.name).join(", ");
    console.log(`${state}: ${value.count}${names ? ` (${names})` : ""}`);
  }
}

function printHumanBatchStatus(summary) {
  console.log(`批次 ${summary.batch_id}: ${summary.state}`);
  console.log(
    `付费批准：${summary.payment_approval_required ? "仍需用户明确批准" : "当前不需要"}`,
  );
  for (const [name, schedule] of Object.entries(summary.schedules || {})) {
    if (!schedule) continue;
    const phases = Array.isArray(schedule.jobs)
      ? schedule.jobs.map((job) => `${job.id}=${job.phase}`).join(", ")
      : "无 Job 记录";
    console.log(`${name} 调度：${schedule.policy || "UNKNOWN"} (${phases})`);
  }
  if (summary.latest_node_process) {
    console.log(
      `最新节点：${summary.latest_node_process.scope} / ${summary.latest_node_process.phase}`,
    );
  }
  for (const job of summary.preparation) {
    console.log(
      `${job.id}: ${job.preparation_status}${job.blocker ? ` (${job.blocker})` : ""}`,
    );
  }
}

export function usage() {
  return `视频替换 JavaScript 批量编排器

用法：
  node tools/video_batch_orchestrator.mjs [全局参数] <command> [batch]

命令：
  init                  初始化批次目录
  once                  扫描一次；默认 shadow，不启动 Codex 或付费任务
  watch                 由 JavaScript 负责循环、退避、锁和健康快照
  status [batch]        查看全局目录，或查看一个批次的真实 workflow state
  check <batch>         检查 requirements.txt 覆盖
  prepare <batch>       在 PAUSE 下完成隔离单 Job 节点 Gate 与本地 preview
  retry-prepare <batch> <V编号>  重跑一个失败或本地提交计划失效的准备 Job
  submit-prepared <batch>  以生成池提交已准备批次（需 --confirm-paid）
  submit-job <batch> <V编号>  只提交一个已准备 Job（需 --confirm-paid）
  retry-sequential <batch> 串行语义重试（需授权 manifest 与 --confirm-paid）

关键参数：
  --config <json>       读取 schema_version=1 的配置
  --confirm-paid        本次进程明确解锁付费路径
  --json                status 输出 JSON
  --root <path>         video-loop 根目录
  --project-root <path> 项目根目录
  --python <path>       固定 Python 3.12 runtime
  --engine <path>       固定 Python 批次引擎
  --preparation-concurrency <n>  本地准备并发（固定 1，严格 FIFO）
  --generation-concurrency <n>   即梦生成并发（默认 2）

节点隔离固定使用外置 0700 state/node-runs 和 sibling workspace/codex-home/transport。
模型节点的 workspace 为零写入、工具与网络关闭；父进程先用可信 ffmpeg 生成并校验帧，
再把帧和 JSON 作为只读输入交给模型；模型仅返回结构化结果，prompt.txt 由父进程落盘。

示例：
  node tools/video_batch_orchestrator.mjs status
  node tools/video_batch_orchestrator.mjs watch
  node tools/video_batch_orchestrator.mjs prepare batch-001
  node tools/video_batch_orchestrator.mjs retry-prepare batch-001 V003
  node tools/video_batch_orchestrator.mjs --confirm-paid submit-job batch-001 V001
`;
}

export async function main(argv = process.argv.slice(2)) {
  const options = await resolveOptions(argv);
  if (options.help || options.command === "help") {
    process.stdout.write(usage());
    return 0;
  }
  if (options.command === "status") {
    const summary = options.batch
      ? await readBatchStatus(
          await resolveBatchPath(options.loopRoot, options.batch),
        )
      : await readLoopSummary(options.loopRoot);
    if (options.json) {
      console.log(JSON.stringify(summary, null, 2));
    } else if (options.batch) {
      printHumanBatchStatus(summary);
    } else {
      printHumanStatus(summary);
    }
    return 0;
  }
  if (options.command === "watch") {
    return runWatch(options);
  }
  let result;
  if (options.command === "once") {
    result = await withOrchestratorLock(options, () => runEngine(options, "once"));
  } else if (options.command === "prepare") {
    const batch = await resolveBatchPath(options.loopRoot, options.batch);
    const prepared = await withOrchestratorLock(options, () =>
      runStreamingBatch(
        // `prepare` is an explicit local-only operation: execute every
        // preparation node but retain preparationOnly so no submit-job path
        // can run and PAUSE continues to block remote work.
        { ...options, execute: true },
        batch,
        { preparationOnly: true },
      ),
    );
    result = {
      code: 0,
      signal: null,
      stdout: `${JSON.stringify(prepared.finalized)}\n`,
      stderr: "",
    };
    process.stdout.write(result.stdout);
  } else if (options.command === "retry-prepare") {
    const batch = await resolveBatchPath(options.loopRoot, options.batch);
    const retried = await withOrchestratorLock(options, async () => {
      const scoped = {
        ...options,
        batch,
        preparationOnly: true,
      };
      const retry = await runWorkerJson(
        { ...scoped, jobId: options.jobId },
        "retry-prepare-job",
      );
      const finalized = await runWorkerJson(scoped, "finalize-preparation-flow");
      return { retry, finalized };
    });
    result = {
      code: 0,
      signal: null,
      stdout: `${JSON.stringify(retried.finalized)}\n`,
      stderr: "",
    };
    process.stdout.write(result.stdout);
  } else if (options.command === "submit-prepared") {
    const batch = await resolveBatchPath(options.loopRoot, options.batch);
    const submitted = await withOrchestratorLock(options, () =>
      runPreparedStreamingBatch(options, batch),
    );
    result = {
      code: 0,
      signal: null,
      stdout: `${JSON.stringify(submitted.finalized)}\n`,
      stderr: "",
    };
    process.stdout.write(result.stdout);
  } else if (options.command === "submit-job") {
    const batch = await resolveBatchPath(options.loopRoot, options.batch);
    const submitted = await withOrchestratorLock(options, () =>
      runPreparedStreamingJob(options, batch, options.jobId),
    );
    result = {
      code: 0,
      signal: null,
      stdout: `${JSON.stringify(submitted.submission)}\n`,
      stderr: "",
    };
    process.stdout.write(result.stdout);
  } else if (options.command === "retry-sequential") {
    result = await withOrchestratorLock(options, async () => {
      const batch = await resolveBatchPath(options.loopRoot, options.batch);
      const scoped = { ...options, batch };
      const flow = await readStoredStreamingFlow(batch);
      await runWorkerJson(scoped, "preflight-submission");
      await writePaymentCheckpoint(scoped, batch, flow);
      return runEngine(scoped, "retry-sequential");
    });
  } else {
    result = await runEngine(options);
  }
  await writeSnapshot(options, {
    command: options.command,
    status: result.code === 0 ? "completed" : "failed",
    started_at: utcNow(),
    last_run: {
      finished_at: utcNow(),
      exit_code: result.code,
      signal: result.signal,
      stdout_tail: tailText(result.stdout),
      stderr_tail: tailText(result.stderr),
    },
  });
  return result.code;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === resolve(SCRIPT_PATH)) {
  try {
    process.exitCode = await main();
  } catch (error) {
    const exitCode = error instanceof OrchestratorError ? error.exitCode : 1;
    console.error(`ERROR: ${error.message}`);
    process.exitCode = exitCode;
  }
}
