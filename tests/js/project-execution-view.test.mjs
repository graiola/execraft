import assert from "node:assert/strict";
import fs from "node:fs";

const view = fs.readFileSync("src/execraft/assets/gui/project-execution-view.js", "utf8");
const html = fs.readFileSync("src/execraft/assets/gui/index.html", "utf8");
const onboarding = fs.readFileSync("src/execraft/assets/gui/onboarding-view.js", "utf8");
const roadmap = fs.readFileSync("src/execraft/assets/gui/roadmap-view.js", "utf8");
const css = fs.readFileSync("src/execraft/assets/gui/gui.css", "utf8");

assert.equal((html.match(/id="projectExecutionView"/g) || []).length, 1);
assert.equal((html.match(/id="projectExecutionReadinessView"/g) || []).length, 1);
assert.match(html, /data-project-view="execution"/);
assert.match(html, /Phases, Gates &amp; Milestones/);
assert.match(html, /Work Package scheduling remains task-local/);
assert.match(html, /class="project-execution-layout"/);
assert.match(html, /class="project-execution-asset-grid"/);
assert.doesNotMatch(html, /class="project-execution-grid"/);
assert.doesNotMatch(html, /class="project-execution-columns"/);
assert.match(onboarding, /new ProjectExecutionView/);
assert.match(onboarding, /\["roadmap", "execution", "tasks", "settings", "archive"\]/);
assert.match(onboarding, /invalidateCanonicalProjection/);
assert.match(onboarding, /projectExecutionView\?\.invalidate/);

assert.match(view, /\/api\/project-execution\/status/);
assert.match(view, /\/api\/project-execution\/task\/start/);
assert.match(view, /\/api\/project-execution\/gate\/decide/);
assert.match(view, /\/api\/project-execution\/gate\/waive/);
assert.match(view, /data-project-asset-delete/);
assert.match(view, /data-project-task-remove/);
assert.match(view, /task_completion/);
assert.match(view, /task_verification/);
assert.match(view, /task_artifact/);
assert.match(view, /project_gate/);
assert.match(view, /human_approval/);
assert.match(view, /invalidate\(\)/);
assert.doesNotMatch(view, /MilestoneDirective|selectedMilestone|activeMilestone/);

assert.match(roadmap, /project_execution_revision/);
assert.match(roadmap, /project_asset_id/);
assert.match(roadmap, /#updateCanonicalAssetMetadata/);
assert.match(roadmap, /\/api\/project-execution\/\$\{item\.kind\}\/metadata/);
assert.match(roadmap, /id="roadmapFitBtn"|roadmapFitBtn/);
assert.match(roadmap, /fittedTimelineDomain/);
assert.match(roadmap, /invalidateCanonicalProjection/);
assert.match(html, /id="roadmapGroupBy"/);
assert.match(html, /id="roadmapCanonicalInitializeDialog"/);
assert.match(html, /Planning relation only/);
assert.match(onboarding, /#openProjectExecutionAsset/);
assert.match(view, /async focusAsset\(kind, assetId\)/);
assert.match(roadmap, /Open in Execution/);
assert.match(roadmap, /expected_project_execution_revision/);
assert.match(roadmap, /dblclick/);
assert.match(roadmap, /#selectItem/);
assert.match(roadmap, /#openItem/);

// GUI-R4 operator workspace: control state is surfaced before canonical CRUD.
assert.match(html, /id="projectExecutionCurrent"/);
assert.match(html, /id="projectExecutionReadyTasks"/);
assert.match(html, /id="projectExecutionBlockedTasks"/);
assert.match(html, /Canonical Project Execution graph/);
assert.match(view, /#renderCurrent\(\)/);
assert.match(view, /#renderQueues\(\)/);
assert.match(view, /Completion requirements/);
assert.match(view, /Current typed evidence/);
assert.match(view, /Advanced · raw immutable baseline/);

// Gate decisions and holds use explicit dialogs instead of browser prompts.
assert.match(html, /id="projectExecutionGateDecisionDialog"/);
assert.match(html, /Evidence fingerprint/);
assert.match(html, /id="projectExecutionHoldDialog"/);
assert.match(view, /#openGateDecisionDialog/);
assert.match(view, /#submitGateDecision/);
assert.match(view, /#submitHold/);
assert.doesNotMatch(view, /window\.prompt/);

// Graph identities are selected from typed canonical references, not CSV IDs.
assert.match(html, /class="project-execution-reference-picker"/);
assert.match(view, /#renderReferencePicker/);
assert.match(view, /#readReferencePicker/);
assert.match(view, /data-gate-criterion-task/);
assert.match(view, /data-gate-criterion-gate/);
assert.doesNotMatch(html, /id="projectExecutionTaskPrerequisites" type="text"/);
assert.doesNotMatch(html, /id="projectExecutionPhaseEntryGates" type="text"/);
assert.match(css, /\.project-execution-queues/);
assert.match(css, /\.project-execution-checklist/);
assert.match(css, /\.project-execution-decision-dialog/);

console.log("Project Execution GUI contracts: PASS");

assert.match(html, /<option value="automatic">Automatic<\/option>/);
assert.match(html, /id="projectExecutionAutomaticCycleBtn"/);
assert.match(html, /id="projectExecutionMaxParallel"/);
assert.match(html, /id="projectExecutionMaxPerPhase"/);
assert.match(html, /id="projectExecutionMaxActivePhases"/);
assert.match(html, /id="projectExecutionFailureBehavior"/);
assert.match(view, /\/api\/project-execution\/policy/);
assert.match(view, /\/api\/project-execution\/automatic\/cycle/);
assert.match(view, /Status reads never start Tasks/);

// GUI-R5 accessibility, responsive-state and rendered-browser hardening contracts.
assert.match(html, /id="roadmapInlineState"[^>]*aria-live="polite"/);
assert.match(html, /id="roadmapTimelineScroll"[^>]*aria-busy="false"/);
assert.match(html, /id="projectExecutionStateBanner"[^>]*aria-live="polite"/);
assert.match(html, /id="projectExecutionWorkspace"[^>]*aria-busy="false"/);
assert.match(html, /projectExecutionGateDecisionDialog[^>]*aria-labelledby="projectExecutionGateDecisionTitle"/);
assert.match(html, /roadmapCanonicalInitializeDialog[^>]*aria-describedby="roadmapCanonicalInitializeMessage"/);
assert.match(roadmap, /function todayDay\(\)/);
assert.match(roadmap, /event\.key === " "|event\.key === "Spacebar"/);
assert.match(roadmap, /aria-pressed="\$\{selected\}"/);
assert.match(roadmap, /#showInlineState/);
assert.match(view, /#showStateBanner/);
assert.match(view, /#isRevisionConflict/);
assert.match(view, /#focusReferencePicker/);
assert.match(css, /\.roadmap-inline-state/);
assert.match(css, /\.project-execution-state-banner/);
assert.match(css, /\.form-dialog form[\s\S]*overflow:\s*auto/);

// GUI-R7 coordination conflicts remain observable and only safe typed actions render.
assert.match(view, /#showCoordinationBanner/);
assert.match(view, /data-project-coordination-action/);
assert.match(view, /\/api\/project-execution\/coordination\/resolve/);
assert.match(view, /No force\/overwrite action is safe/);
assert.match(roadmap, /#showCoordinationState/);
assert.match(roadmap, /Open Project Execution for typed recovery details/);

// GUI-R8 read-only coordination forensics and journal history.
assert.match(html, /id="projectExecutionCoordinationInspector"/);
assert.match(html, /Coordination log/);
assert.match(view, /Inspect three-way state/);
assert.match(view, /#renderCoordinationInspector/);
assert.match(view, /#forensicDomain/);
assert.match(view, /\/api\/project-execution\/coordination\/history/);
assert.match(css, /\.project-execution-forensic-subject/);
assert.doesNotMatch(view, /merge anyway|force overwrite|rollback to before/i);
