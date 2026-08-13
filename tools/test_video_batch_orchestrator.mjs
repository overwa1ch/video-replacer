import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  OrchestratorError,
  PAYMENT_AUTH_TOKEN_ENV,
  STATE_NAMES,
  buildEngineArgs,
  main,
  readLoopSummary,
  paymentAuthorizationBinding,
  resolveOptions,
  runEngine,
  runJobPipeline,
  runPreparedStreamingBatch,
  runWatch,
} from "./video_batch_orchestrator.mjs";

const TEST_REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "video-batch-orchestrator-"));
  const projectRoot = join(root, "project");
  const loopRoot = join(root, "video-loop");
  const engine = join(root, "fake-engine.mjs");
  await mkdir(projectRoot, { recursive: true });
  await writeFile(
    engine,
    [
      "const args = process.argv.slice(2);",
      "console.log(JSON.stringify({args,payment_token_present:Boolean(process.env.VIDEO_LOOP_PAYMENT_AUTH_TOKEN)}));",
      "process.exit(Number(process.env.FAKE_ENGINE_EXIT || 0));",
      "",
    ].join("\n"),
    "utf8",
  );
  return { root, projectRoot, loopRoot, engine };
}

async function optionsFor(paths, extra = []) {
  return resolveOptions([
    "--project-root",
    paths.projectRoot,
    "--root",
    paths.loopRoot,
    "--python",
    process.execPath,
    "--engine",
    paths.engine,
    ...extra,
  ]);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function writeProfileManagedPreparedJob(paths, batch, jobId) {
  const outputDir = join(
    paths.projectRoot,
    "outputs",
    "video-replacements",
    `${basename(batch)}-${jobId}`,
  );
  await mkdir(outputDir, { recursive: true });
  const finalVideo = join(outputDir, "source-upload-ready.mp4");
  const finalBytes = Buffer.from(`final-video-${jobId}`);
  await writeFile(finalVideo, finalBytes);
  const uploadManifest = join(outputDir, "upload-preparation.json");
  await writeFile(
    uploadManifest,
    JSON.stringify({ schema_version: 1, job_id: jobId }),
    "utf8",
  );
  const uploadManifestSha = sha256(await readFile(uploadManifest));
  const preflightManifest = join(outputDir, "preflight.json");
  const preflight = {
    schema_version: 1,
    backend_profile: "dreamina_cli_seedance_2_5",
    constraints_digest: "a".repeat(64),
    model_version: "seedance2.5",
    upload_preparation_manifest: uploadManifest,
    upload_preparation_manifest_sha256: uploadManifestSha,
    active_video: { path: finalVideo, sha256: sha256(finalBytes) },
  };
  await writeFile(preflightManifest, JSON.stringify(preflight), "utf8");
  const preflightManifestSha = sha256(await readFile(preflightManifest));
  await writeFile(
    join(outputDir, "submission-plan.json"),
    JSON.stringify({
      schema_version: 2,
      batch_id: basename(batch),
      job_id: jobId,
      backend_profile: "dreamina_cli_seedance_2_5",
      backend_profile_constraints_sha256: "a".repeat(64),
      preflight_manifest: preflightManifest,
      preflight_manifest_sha256: preflightManifestSha,
      upload_preparation_manifest: uploadManifest,
    }),
    "utf8",
  );
  return { finalVideo, finalVideoSha: sha256(finalBytes) };
}

test("shadow once maps JavaScript controls to one Python-engine scan", async () => {
  const paths = await fixture();
  const options = await optionsFor(paths, ["--stable-seconds", "7", "once"]);
  const args = buildEngineArgs(options);
  assert.equal(args[0], paths.engine);
  assert.deepEqual(args.slice(-3), ["once", "--stable-seconds", "7"]);
  assert.equal(args.includes("--execute"), false);
  assert.equal(args.includes("--auto-ready"), false);

  const previousToken = process.env[PAYMENT_AUTH_TOKEN_ENV];
  process.env[PAYMENT_AUTH_TOKEN_ENV] = "f".repeat(64);
  let result;
  try {
    result = await runEngine(options, "once", { stream: false });
  } finally {
    if (previousToken === undefined) delete process.env[PAYMENT_AUTH_TOKEN_ENV];
    else process.env[PAYMENT_AUTH_TOKEN_ENV] = previousToken;
  }
  assert.equal(result.code, 0);
  assert.match(result.stdout, /"once"/);
  assert.equal(JSON.parse(result.stdout).payment_token_present, false);
});

test("an explicit project root relocates runtime but not the repository-owned engine", async () => {
  const paths = await fixture();
  const options = await resolveOptions([
    "--project-root",
    paths.projectRoot,
    "status",
  ]);
  assert.equal(options.projectRoot, paths.projectRoot);
  assert.equal(
    options.engine,
    join(TEST_REPO_ROOT, "tools", "video_batch_loop.py"),
  );
  assert.notEqual(
    options.engine,
    join(paths.projectRoot, "tools", "video_batch_loop.py"),
  );
  assert.equal(options.loopRoot, join(paths.projectRoot, "workspace", "video-loop"));
});

test("standalone defaults keep runtime and Python inside the cloned repository", async () => {
  const options = await resolveOptions(["status"], {});
  assert.equal(options.projectRoot, TEST_REPO_ROOT);
  assert.equal(options.loopRoot, join(options.projectRoot, "workspace", "video-loop"));
  if (process.platform === "win32") {
    assert.equal(options.python, join(options.projectRoot, ".venv", "Scripts", "python.exe"));
  } else {
    assert.equal(options.python, join(options.projectRoot, ".venv", "bin", "python3"));
  }
});

test("paid authorization is limited to explicit batch submission", async () => {
  const paths = await fixture();
  await assert.rejects(
    optionsFor(paths, ["--execute", "watch"]),
    (error) => error instanceof OrchestratorError && /未知参数/.test(error.message),
  );
  await assert.rejects(
    optionsFor(paths, ["submit-prepared", "batch-a"]),
    (error) => error instanceof OrchestratorError && /--confirm-paid/.test(error.message),
  );
  for (const command of ["once", "watch"]) {
    await assert.rejects(
      optionsFor(paths, ["--confirm-paid", command]),
      (error) => error instanceof OrchestratorError && /只适用于/.test(error.message),
    );
  }
  const paid = await optionsFor(paths, [
    "--confirm-paid",
    "submit-prepared",
    "batch-a",
  ]);
  assert.match(paid.paymentAuthorizationToken, /^[0-9a-f]{64}$/);
  const secondPaid = await optionsFor(paths, [
    "--confirm-paid",
    "submit-prepared",
    "batch-a",
  ]);
  assert.notEqual(
    paid.paymentAuthorizationToken,
    secondPaid.paymentAuthorizationToken,
  );
});

test("status reports batch folders without invoking the worker", async () => {
  const paths = await fixture();
  for (const state of STATE_NAMES) {
    await mkdir(join(paths.loopRoot, state), { recursive: true });
  }
  await mkdir(join(paths.loopRoot, "needs-input", "batch-a"));
  await mkdir(join(paths.loopRoot, "review", "batch-b"));
  const summary = await readLoopSummary(paths.loopRoot);
  assert.equal(summary.states["needs-input"].count, 1);
  assert.equal(summary.states.review.entries[0].name, "batch-b");
  assert.equal(summary.orchestrator, null);
});

test("JavaScript watch owns cycles and persists a health snapshot", async () => {
  const paths = await fixture();
  const options = await optionsFor(paths, [
    "--max-cycles",
    "1",
    "--max-consecutive-failures",
    "2",
    "watch",
  ]);
  const exitCode = await runWatch(options, { stream: false });
  assert.equal(exitCode, 0);
  const snapshot = JSON.parse(
    await readFile(join(paths.loopRoot, "logs", "js-orchestrator-state.json"), "utf8"),
  );
  assert.equal(snapshot.orchestrator, "javascript");
  assert.equal(snapshot.status, "completed");
  assert.equal(snapshot.mode, "shadow");
  assert.equal(snapshot.cycle, 1);
});

test("streaming pipeline enforces separate preparation and generation pools", async () => {
  const jobs = Array.from({ length: 7 }, (_, index) => ({ id: `V${String(index + 1).padStart(3, "0")}` }));
  let preparationActive = 0;
  let generationActive = 0;
  let maxPreparation = 0;
  let maxGeneration = 0;
  let preparedCount = 0;
  let preparedWhenFirstGenerationStarted = null;
  const wait = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));

  const results = await runJobPipeline(jobs, {
    preparationConcurrency: 3,
    generationConcurrency: 2,
    execute: true,
    prepareJob: async (job) => {
      preparationActive += 1;
      maxPreparation = Math.max(maxPreparation, preparationActive);
      await wait(8);
      preparationActive -= 1;
      preparedCount += 1;
      return { job: { id: job.id, status: "READY_FOR_SUBMISSION" } };
    },
    submitJob: async (job) => {
      if (preparedWhenFirstGenerationStarted === null) {
        preparedWhenFirstGenerationStarted = preparedCount;
      }
      generationActive += 1;
      maxGeneration = Math.max(maxGeneration, generationActive);
      await wait(18);
      generationActive -= 1;
      return { job: { id: job.id, status: "COMPLETED" } };
    },
  });

  assert.equal(results.length, 7);
  assert.equal(maxPreparation, 3);
  assert.equal(maxGeneration, 2);
  assert.ok(preparedWhenFirstGenerationStarted < jobs.length);
});

test("prepare executes local preparation nodes and never reaches submission", async () => {
  const paths = await fixture();
  const batch = join(paths.loopRoot, "needs-input", "batch-local-only");
  const commandLog = join(paths.root, "commands.log");
  await mkdir(batch, { recursive: true });
  await writeFile(
    paths.engine,
    [
      "import { appendFileSync } from 'node:fs';",
      "const args = process.argv.slice(2);",
      "const known = ['cleanup-node-runs', 'inspect-flow', 'prepare-job', 'submit-job', 'finalize-preparation-flow'];",
      "const command = args.find((value) => known.includes(value));",
      `appendFileSync(${JSON.stringify(commandLog)}, command + '\\n');`,
      "const index = args.indexOf(command);",
      "const jobId = command === 'prepare-job' ? args[index + 2] : null;",
      "if (command === 'cleanup-node-runs') console.log(JSON.stringify({removed_count:0,removed:[]}));",
      "else if (command === 'inspect-flow') console.log(JSON.stringify({jobs:[{id:'V001'}]}));",
      "else if (command === 'prepare-job') console.log(JSON.stringify({job:{id:jobId,status:'READY_FOR_SUBMISSION'}}));",
      "else if (command === 'finalize-preparation-flow') console.log(JSON.stringify({destination:'needs-input/batch-local-only'}));",
      "else process.exit(3);",
      "",
    ].join("\n"),
    "utf8",
  );
  const exitCode = await main([
    "--project-root",
    paths.projectRoot,
    "--root",
    paths.loopRoot,
    "--python",
    process.execPath,
    "--engine",
    paths.engine,
    "prepare",
    batch,
  ]);
  assert.equal(exitCode, 0);
  assert.deepEqual(
    (await readFile(commandLog, "utf8")).trim().split("\n"),
    ["cleanup-node-runs", "inspect-flow", "prepare-job", "finalize-preparation-flow"],
  );
});

test("submit-prepared stays on the streaming generation path", async () => {
  const paths = await fixture();
  const batch = join(paths.loopRoot, "needs-input", "batch-prepared");
  await mkdir(batch, { recursive: true });
  await writeFile(
    paths.engine,
    [
      "const args = process.argv.slice(2);",
      "const known = ['cleanup-node-runs', 'inspect-flow', 'prepare-job', 'preflight-submission', 'submit-job', 'finalize-flow'];",
      "const command = args.find((value) => known.includes(value));",
      "const index = args.indexOf(command);",
      "const jobId = command === 'prepare-job' || command === 'submit-job' ? args[index + 2] : null;",
      "if (command === 'cleanup-node-runs') console.log(JSON.stringify({removed_count:0,removed:[]}));",
      "else if (command === 'inspect-flow') console.log(JSON.stringify({flow_fingerprint:'flow-a', jobs:[{id:'V001'},{id:'V002'}]}));",
      "else if (command === 'prepare-job') console.log(JSON.stringify({job:{id:jobId,status:'READY_FOR_SUBMISSION'}}));",
      "else if (command === 'preflight-submission') console.log(JSON.stringify({ready_for_paid_submission:true}));",
      "else if (command === 'submit-job') { if (!/^[0-9a-f]{64}$/.test(process.env.VIDEO_LOOP_PAYMENT_AUTH_TOKEN || '')) process.exit(4); console.log(JSON.stringify({job:{id:jobId,status:'COMPLETED'}})); }",
      "else if (command === 'finalize-flow') console.log(JSON.stringify({destination:'completed/batch-prepared'}));",
      "else process.exit(3);",
      "",
    ].join("\n"),
    "utf8",
  );
  const options = await optionsFor(paths, [
    "--confirm-paid",
    "submit-prepared",
    "batch-prepared",
  ]);
  const result = await runPreparedStreamingBatch(options, batch);
  assert.equal(result.results.length, 2);
  assert.equal(
    result.results.every((item) => item.submission.job.status === "COMPLETED"),
    true,
  );
  const checkpoint = JSON.parse(
    await readFile(join(batch, "payment-checkpoint.json"), "utf8"),
  );
  assert.equal(checkpoint.flow_fingerprint, "flow-a");
  assert.equal(checkpoint.planned_paid_tasks, 2);
  assert.equal(checkpoint.schema_version, 2);
  assert.equal(checkpoint.authorization_scope, "current-batch");
  assert.equal(checkpoint.orchestrator_pid, process.pid);
  assert.match(checkpoint.authorization_nonce, /^[0-9a-f]{32}$/);
  assert.equal(
    checkpoint.authorization_binding_sha256,
    paymentAuthorizationBinding(options.paymentAuthorizationToken, checkpoint),
  );
  assert.equal(JSON.stringify(checkpoint).includes(options.paymentAuthorizationToken), false);
});

test("submit-prepared preflights the whole prepared batch before any paid checkpoint", async () => {
  const paths = await fixture();
  const batch = join(paths.loopRoot, "needs-input", "batch-preflight-blocked");
  const eventLog = join(paths.root, "preflight-events.log");
  await mkdir(batch, { recursive: true });
  await writeFile(
    paths.engine,
    [
      "import { appendFileSync } from 'node:fs';",
      "const args = process.argv.slice(2);",
      "const known = ['inspect-flow', 'prepare-job', 'preflight-submission', 'submit-job', 'finalize-flow'];",
      "const command = args.find((value) => known.includes(value));",
      "const index = args.indexOf(command);",
      "const jobId = command === 'prepare-job' || command === 'submit-job' ? args[index + 2] : null;",
      "if (command === 'inspect-flow') console.log(JSON.stringify({flow_fingerprint:'flow-preflight',jobs:[{id:'V001'},{id:'V002'},{id:'V003'}]}));",
      `else if (command === 'prepare-job') { appendFileSync(${JSON.stringify(eventLog)}, 'prepare-done:' + jobId + '\\n'); console.log(JSON.stringify({job:{id:jobId,status:'READY_FOR_SUBMISSION'}})); }`,
      `else if (command === 'preflight-submission') { appendFileSync(${JSON.stringify(eventLog)}, 'preflight-submission\\n'); process.stderr.write('invalid submission plan\\n'); process.exit(9); }`,
      `else if (command === 'submit-job') { appendFileSync(${JSON.stringify(eventLog)}, 'submit-job:' + jobId + '\\n'); console.log(JSON.stringify({job:{id:jobId,status:'COMPLETED'}})); }`,
      `else if (command === 'finalize-flow') { appendFileSync(${JSON.stringify(eventLog)}, 'finalize-flow\\n'); console.log(JSON.stringify({destination:'completed/batch-preflight-blocked'})); }`,
      "else process.exit(3);",
      "",
    ].join("\n"),
    "utf8",
  );
  const options = await optionsFor(paths, [
    "--confirm-paid",
    "submit-prepared",
    "batch-preflight-blocked",
  ]);

  await assert.rejects(runPreparedStreamingBatch(options, batch));

  const events = (await readFile(eventLog, "utf8")).trim().split("\n");
  const preparationEvents = events.filter((event) =>
    event.startsWith("prepare-done:"),
  );
  assert.deepEqual(
    [...preparationEvents].sort(),
    ["prepare-done:V001", "prepare-done:V002", "prepare-done:V003"],
  );
  assert.equal(
    events.filter((event) => event === "preflight-submission").length,
    1,
  );
  assert.ok(
    preparationEvents.every(
      (event) => events.indexOf(event) < events.indexOf("preflight-submission"),
    ),
  );
  assert.equal(events.some((event) => event.startsWith("submit-job:")), false);
  assert.equal(events.includes("finalize-flow"), false);
  await assert.rejects(readFile(join(batch, "payment-checkpoint.json"), "utf8"));
});

test("retry-sequential preflights before checkpoint creation and engine retry", async () => {
  const paths = await fixture();
  const eventLog = join(paths.root, "retry-sequential-events.log");
  await writeFile(
    paths.engine,
    [
      "import { appendFileSync, existsSync } from 'node:fs';",
      "import { join } from 'node:path';",
      "const args = process.argv.slice(2);",
      "const known = ['cleanup-node-runs', 'preflight-submission', 'retry-sequential'];",
      "const command = args.find((value) => known.includes(value));",
      "const commandIndex = args.indexOf(command);",
      "const batch = args[commandIndex + 1] || '';",
      "const checkpoint = join(batch, 'payment-checkpoint.json');",
      "if (command === 'cleanup-node-runs') console.log(JSON.stringify({removed_count:0,removed:[]}));",
      `else if (command === 'preflight-submission') { appendFileSync(${JSON.stringify(eventLog)}, 'preflight:' + (existsSync(checkpoint) ? 'checkpoint-present' : 'no-checkpoint') + '\\n'); if (batch.includes('blocked')) { process.stderr.write('invalid submission plan\\n'); process.exit(9); } console.log(JSON.stringify({ready_for_paid_submission:true})); }`,
      `else if (command === 'retry-sequential') { appendFileSync(${JSON.stringify(eventLog)}, 'retry:' + (existsSync(checkpoint) ? 'checkpoint-present' : 'no-checkpoint') + '\\n'); console.log(JSON.stringify({retried:true})); }`,
      "else process.exit(3);",
      "",
    ].join("\n"),
    "utf8",
  );

  async function retryFixture(name) {
    const batch = join(paths.loopRoot, "needs-input", name);
    const authorizationManifest = join(paths.root, `${name}-authorization.json`);
    await mkdir(batch, { recursive: true });
    await writeFile(
      join(batch, "streaming-flow.json"),
      JSON.stringify({
        schema_version: 1,
        batch_id: name,
        flow_fingerprint: "a".repeat(64),
        jobs: [{ id: "V001", skipped: false }],
      }),
      "utf8",
    );
    await writeFile(authorizationManifest, JSON.stringify({ approved: true }), "utf8");
    return { batch, authorizationManifest };
  }

  const allowed = await retryFixture("batch-retry-allowed");
  const allowedExitCode = await main([
    "--project-root",
    paths.projectRoot,
    "--root",
    paths.loopRoot,
    "--python",
    process.execPath,
    "--engine",
    paths.engine,
    "--confirm-paid",
    "--retry-authorization-manifest",
    allowed.authorizationManifest,
    "retry-sequential",
    "batch-retry-allowed",
  ]);
  assert.equal(allowedExitCode, 0);
  assert.deepEqual(
    (await readFile(eventLog, "utf8")).trim().split("\n"),
    ["preflight:no-checkpoint", "retry:checkpoint-present"],
  );
  await readFile(join(allowed.batch, "payment-checkpoint.json"), "utf8");

  const blocked = await retryFixture("batch-retry-blocked");
  await assert.rejects(
    main([
      "--project-root",
      paths.projectRoot,
      "--root",
      paths.loopRoot,
      "--python",
      process.execPath,
      "--engine",
      paths.engine,
      "--confirm-paid",
      "--retry-authorization-manifest",
      blocked.authorizationManifest,
      "retry-sequential",
      "batch-retry-blocked",
    ]),
    (error) =>
      error instanceof OrchestratorError && /invalid submission plan/.test(error.message),
  );
  assert.deepEqual(
    (await readFile(eventLog, "utf8")).trim().split("\n"),
    [
      "preflight:no-checkpoint",
      "retry:checkpoint-present",
      "preflight:no-checkpoint",
    ],
  );
  await assert.rejects(
    readFile(join(blocked.batch, "payment-checkpoint.json"), "utf8"),
  );
});

test("profile-managed submission approval binds each job's final local media", async () => {
  const paths = await fixture();
  const batch = join(paths.loopRoot, "needs-input", "batch-profile-prepared");
  await mkdir(batch, { recursive: true });
  await writeFile(
    join(batch, "job-bindings.json"),
    JSON.stringify({
      schema_version: 3,
      batch_id: "batch-profile-prepared",
      backend_profile: "dreamina_cli_seedance_2_5",
      jobs: [
        { id: "V001", privacy_mode: "none", references: [] },
        { id: "V002", privacy_mode: "none", references: [] },
      ],
    }),
    "utf8",
  );
  const first = await writeProfileManagedPreparedJob(paths, batch, "V001");
  const second = await writeProfileManagedPreparedJob(paths, batch, "V002");
  await writeFile(
    paths.engine,
    [
      "const args = process.argv.slice(2);",
      "const known = ['cleanup-node-runs', 'inspect-flow', 'prepare-job', 'preflight-submission', 'submit-job', 'finalize-flow'];",
      "const command = args.find((value) => known.includes(value));",
      "const index = args.indexOf(command);",
      "const jobId = command === 'prepare-job' || command === 'submit-job' ? args[index + 2] : null;",
      "if (command === 'cleanup-node-runs') console.log(JSON.stringify({removed_count:0,removed:[]}));",
      "else if (command === 'inspect-flow') console.log(JSON.stringify({flow_fingerprint:'profile-flow',jobs:[{id:'V001'},{id:'V002'}]}));",
      "else if (command === 'prepare-job') console.log(JSON.stringify({job:{id:jobId,status:'READY_FOR_SUBMISSION'}}));",
      "else if (command === 'preflight-submission') console.log(JSON.stringify({ready_for_paid_submission:true}));",
      "else if (command === 'submit-job') console.log(JSON.stringify({job:{id:jobId,status:'COMPLETED'}}));",
      "else if (command === 'finalize-flow') console.log(JSON.stringify({destination:'completed/batch-profile-prepared'}));",
      "else process.exit(3);",
      "",
    ].join("\n"),
    "utf8",
  );
  const options = await optionsFor(paths, [
    "--confirm-paid",
    "submit-prepared",
    "batch-profile-prepared",
  ]);
  await runPreparedStreamingBatch(options, batch);

  for (const [jobId, finalVideoSha] of [
    ["V001", first.finalVideoSha],
    ["V002", second.finalVideoSha],
  ]) {
    const checkpoint = JSON.parse(
      await readFile(join(batch, `payment-checkpoint-${jobId}.json`), "utf8"),
    );
    assert.equal(checkpoint.schema_version, 3);
    assert.equal(checkpoint.authorization_scope, "current-batch-single-job");
    assert.deepEqual(checkpoint.authorized_job_ids, [jobId]);
    assert.equal(checkpoint.submission_identities.length, 1);
    assert.equal(checkpoint.submission_identities[0].job_id, jobId);
    assert.equal(checkpoint.submission_identities[0].final_video_sha256, finalVideoSha);
    assert.equal(
      checkpoint.authorization_binding_sha256,
      paymentAuthorizationBinding(options.paymentAuthorizationToken, checkpoint),
    );
  }
  await assert.rejects(readFile(join(batch, "payment-checkpoint.json"), "utf8"));
});

test("invalid worker JSON blocks before preparation or paid submission", async () => {
  const paths = await fixture();
  const batch = join(paths.loopRoot, "needs-input", "batch-invalid-json");
  const commandLog = join(paths.root, "invalid-json-commands.log");
  await mkdir(batch, { recursive: true });
  await writeFile(
    paths.engine,
    [
      "import { appendFileSync } from 'node:fs';",
      "const args = process.argv.slice(2);",
      "const known = ['cleanup-node-runs', 'inspect-flow', 'prepare-job', 'submit-job'];",
      "const command = args.find((value) => known.includes(value));",
      `appendFileSync(${JSON.stringify(commandLog)}, command + '\\n');`,
      "if (command === 'cleanup-node-runs') console.log(JSON.stringify({removed_count:0,removed:[]}));",
      "else if (command === 'inspect-flow') console.log('progress\\n' + JSON.stringify({flow_fingerprint:'flow-a',jobs:[{id:'V001'}]}));",
      "else console.log(JSON.stringify({job:{id:'V001',status:'COMPLETED'}}));",
      "",
    ].join("\n"),
    "utf8",
  );

  await assert.rejects(
    main([
      "--project-root",
      paths.projectRoot,
      "--root",
      paths.loopRoot,
      "--python",
      process.execPath,
      "--engine",
      paths.engine,
      "--confirm-paid",
      "submit-prepared",
      "batch-invalid-json",
    ]),
    (error) => error instanceof OrchestratorError && /没有返回有效 JSON/.test(error.message),
  );
  assert.deepEqual(
    (await readFile(commandLog, "utf8")).trim().split("\n"),
    ["cleanup-node-runs", "inspect-flow"],
  );
  await assert.rejects(readFile(join(batch, "payment-checkpoint.json"), "utf8"));
});
