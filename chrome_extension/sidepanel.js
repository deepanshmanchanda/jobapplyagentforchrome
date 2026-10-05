const elements = {
  pageName: document.getElementById("page-name"),
  intro: document.getElementById("intro"),
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  autoSubmit: document.getElementById("auto-submit"),
  autoRegister: document.getElementById("auto-register"),
  instruction: document.getElementById("agent-instruction"),
  rememberInstruction: document.getElementById("remember-instruction"),
  sendInstruction: document.getElementById("send-instruction"),
  instructionFeedback: document.getElementById("instruction-feedback"),
  statusDot: document.getElementById("status-dot"),
  statusTitle: document.getElementById("status-title"),
  statusReason: document.getElementById("status-reason"),
  facts: document.getElementById("facts"),
  step: document.getElementById("step"),
  fieldCount: document.getElementById("field-count"),
  progress: document.getElementById("progress"),
  errors: document.getElementById("errors"),
  otherRuns: document.getElementById("other-runs"),
  technicalNote: document.getElementById("technical-note"),
};

let busy = false;
let instructionBusy = false;
const AUTO_SUBMIT_PREFERENCE = "applicationAutopilotAutoSubmit";
const AUTO_REGISTER_PREFERENCE = "applicationAutopilotAutoRegister";

async function restorePreferences() {
  elements.autoSubmit.disabled = true;
  elements.autoRegister.disabled = true;
  try {
    const stored = await chrome.storage.local.get(AUTO_SUBMIT_PREFERENCE);
    elements.autoSubmit.checked = stored[AUTO_SUBMIT_PREFERENCE] !== false;
    const registration = await chrome.storage.local.get(AUTO_REGISTER_PREFERENCE);
    elements.autoRegister.checked = registration[AUTO_REGISTER_PREFERENCE] !== false;
  } catch (_error) {
    // Keep the visible default when preference storage is unavailable.
  } finally {
    elements.autoSubmit.disabled = false;
    elements.autoRegister.disabled = false;
  }
}

function prettyState(state) {
  const labels = {
    off: "Ready when you are",
    waiting_for_snapshot: "Connecting to this page",
    running: "Applying in this tab",
    thinking: "Assessing the page",
    interpreting_instruction: "Understanding your instruction",
    waiting_for_navigation: "Waiting for the page",
    waiting_for_submission: "Waiting for submission confirmation",
    submitted: "Application submitted",
    ready_to_submit: "Application ready to submit",
    needs_user_input: "A missing answer needs your attention",
    action_failed: "Application step needs attention",
    submission_unconfirmed: "Submission not confirmed",
    paused_for_access: "Complete access in Chrome",
    paused: "Paused for your review",
    complete: "Ready for final review",
  };
  return labels[state] || "Checking this page";
}

function dotClass(state, error) {
  if (error) return "error";
  if (state === "submitted") return "complete";
  if (state === "waiting_for_submission") return "waiting_for_navigation";
  if (["action_failed", "submission_unconfirmed"].includes(state)) return "needs_user_input";
  return String(state || "waiting").replace(/[^a-z_]/g, "") || "waiting";
}

function setBusy(nextBusy, label = "Starting...") {
  busy = nextBusy;
  elements.start.disabled = nextBusy;
  elements.stop.disabled = nextBusy;
  elements.sendInstruction.disabled = nextBusy || instructionBusy;
  if (nextBusy) elements.start.textContent = label;
  else elements.start.textContent = "Start Autopilot";
}

function setInstructionBusy(nextBusy) {
  instructionBusy = nextBusy;
  elements.sendInstruction.disabled = nextBusy || busy;
  elements.instruction.disabled = nextBusy;
  elements.rememberInstruction.disabled = nextBusy;
  if (nextBusy) elements.sendInstruction.textContent = "Applying instruction...";
  else elements.sendInstruction.textContent = "Use instruction and continue";
}

function showInstructionFeedback(message = "", error = false) {
  elements.instructionFeedback.hidden = !message;
  elements.instructionFeedback.textContent = message;
  elements.instructionFeedback.classList.toggle("error", Boolean(error));
}

function showTechnical(message = "") {
  elements.technicalNote.hidden = !message;
  elements.technicalNote.textContent = message;
}

function render(panel) {
  const autopilot = panel?.autopilot || { state: "off", reason: panel?.message || "Open an application page to begin." };
  const state = autopilot.state || "off";
  const error = panel?.error || "";
  const tab = panel?.tab;
  const snapshot = panel?.snapshot;

  elements.pageName.textContent = tab?.title || "Open an application page to begin.";
  elements.intro.textContent = panel?.paired && panel?.isCurrentTab === false
    ? `The visible browser page is ${panel?.activeTab?.title || "a different tab"}. Start Autopilot will attach the agent to that current page.`
    : panel?.paired
      ? "This agent is attached only to the page named above. It will not change tabs."
      : "Start Autopilot on an application page to fill saved details, upload your resume, and write answers grounded in your experience.";
  elements.statusDot.className = `status-dot ${dotClass(state, error)}`;
  elements.statusTitle.textContent = error ? "Local agent unavailable" : prettyState(state);
  elements.statusReason.textContent = error || autopilot.reason || "Waiting for your instruction.";
  if (!error && autopilot.modelDecision && ['running', 'waiting_for_navigation'].includes(autopilot.state)) {
    elements.statusReason.textContent += ` ${autopilot.modelDecision}`;
  }
  elements.stop.hidden = state === "off" || state === "submitted" || !panel?.paired;
  elements.facts.hidden = !snapshot && !panel?.paired;
  elements.step.textContent = snapshot?.step || "Waiting for a form";
  elements.fieldCount.textContent = snapshot ? String(snapshot.fieldCount || 0) : "-";
  elements.progress.textContent = autopilot.maxSteps
    ? `${autopilot.steps || 0} of ${autopilot.maxSteps}`
    : "Not started";
  const errors = (snapshot?.errors || []).filter(Boolean).slice(0, 2);
  elements.errors.hidden = !errors.length;
  elements.errors.textContent = errors.length ? `Page message: ${errors.join("; ")}` : "";
  const otherRuns = Math.max(0, Number(panel?.managedCount || 0) - (panel?.paired ? 1 : 0));
  elements.otherRuns.hidden = !otherRuns;
  elements.otherRuns.textContent = otherRuns
    ? `${otherRuns} other application${otherRuns === 1 ? " is" : "s are"} running independently.`
    : "";
  showTechnical("");
}

async function request(type, payload = {}) {
  const result = await chrome.runtime.sendMessage({ type, ...payload });
  if (!result?.ok) throw new Error(result?.message || "The local application agent did not respond.");
  return result;
}

async function refresh() {
  if (busy) return;
  try {
    const result = await request("panel-status");
    render(result.panel);
  } catch (error) {
    render({ error: error.message });
    showTechnical("Keep the local application agent running, then try again.");
  }
}

elements.start.addEventListener("click", async () => {
  setBusy(true);
  showTechnical("");
  try {
    await preferencesReady;
    const result = await request("panel-start-autopilot", {
      maxSteps: 25,
      autoSubmit: elements.autoSubmit.checked,
      autoRegister: elements.autoRegister.checked,
    });
    render(result.panel);
  } catch (error) {
    render({ error: error.message });
    showTechnical("The current page was not changed. Check that the local agent is running and this is an employer application page.");
  } finally {
    setBusy(false);
  }
});

elements.autoSubmit.addEventListener("change", async () => {
  try {
    await chrome.storage.local.set({ [AUTO_SUBMIT_PREFERENCE]: elements.autoSubmit.checked });
  } catch (_error) {
    showTechnical("This submission preference will apply to this run, but could not be saved for next time.");
  }
});

elements.autoRegister.addEventListener("change", async () => {
  try {
    await chrome.storage.local.set({ [AUTO_REGISTER_PREFERENCE]: elements.autoRegister.checked });
  } catch (_error) {
    showTechnical("The account creation preference applies to this run but could not be saved.");
  }
});

elements.stop.addEventListener("click", async () => {
  setBusy(true, "Stopping...");
  try {
    const result = await request("panel-stop-autopilot");
    render(result.panel);
  } catch (error) {
    render({ error: error.message });
  } finally {
    setBusy(false);
  }
});

elements.sendInstruction.addEventListener("click", async () => {
  const instruction = elements.instruction.value.trim();
  if (!instruction) {
    showInstructionFeedback("Type Continue, or Answer [exact visible field label]: [answer].", true);
    return;
  }
  setInstructionBusy(true);
  showInstructionFeedback("");
  try {
    const result = await request("panel-instruction-autopilot", {
      instruction,
      remember: elements.rememberInstruction.checked,
    });
    render(result.panel);
    // Do not keep answers in a permanently open browser panel after the
    // local bridge accepts them.  The bridge stores only validated answers.
    elements.instruction.value = "";
    showInstructionFeedback(result.message || "Instruction accepted. Rechecking the current page.");
  } catch (error) {
    showInstructionFeedback(error.message || "The instruction could not be used on this page.", true);
  } finally {
    setInstructionBusy(false);
  }
});

const preferencesReady = restorePreferences();
void refresh();
setInterval(() => { void refresh(); }, 2000);
