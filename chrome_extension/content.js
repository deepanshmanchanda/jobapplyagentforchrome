(() => {
const CONTENT_BUILD = chrome.runtime?.getManifest?.().version || "unknown";
const BRIDGE_PROTOCOL = 8;
const INSTALL_KEY = "__jobApplicationCopilotBridgeInstaller";
const previousInstaller = globalThis[INSTALL_KEY];
if (previousInstaller?.build === CONTENT_BUILD) return;
previousInstaller?.dispose?.();
let disposed = false;

const PROTECTED = ["password", "passcode", "one time", "otp", "verification", "captcha", "sign in", "log in", "login", "create account", "submit", "send application", "complete application", "national insurance"];
const MANUAL_ONLY = [
  ...PROTECTED,
  "social security", "user name", "username", "final submit",
  "legally entitled", "right to work", "work authorisation", "work authorization",
  "immigration", "visa", "sponsorship", "sponsor", "citizenship",
  "criminal conviction", "criminal record", "conviction",
  "ethnic", "ethnicity", "race", "religion", "disability", "reasonable adjustment",
  "gender", "sex", "sexual orientation", "marital", "marriage", "civil partnership",
  "date of birth", "birth date", "age", "veteran", "military service",
  "referee", "reference contact", "reference details",
  "salary", "compensation", "bank account", "tax code",
];
const SAFE_MEMORY_CONTROLS = new Set(["text", "textarea", "email", "tel", "url", "search", "number"]);
// Bounded non-final application progression. Account, access, and final-submit
// controls are separately blocked by PROTECTED and never match this expression.
const SAFE_CLICK = /^(apply manually|apply|apply for (?:this|the) (?:job|role|position)|apply now|apply online|start (?:your )?application|save\s*(?:&|and)\s*next|save and continue|next(?: step)?|continue(?: application)?)$/i;
const ENTRY_ACTION = /^(apply|apply for (?:this|the) (?:job|role|position)|apply now|apply online|start (?:your )?application)$/i;
const FINAL_ACTION = /^(submit(?: application)?|send application|complete application)$/i;
const ACCESS_ACTION = /^(sign in|log in|login)$/i;
const REGISTER_ACTION = /^(create (?:an? |new )?account|register|sign up|signup|create candidate account)$/i;
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
let generatedSelector = 0;
const submittedActions = previousInstaller?.submittedActions || new Set();
const authenticatedActions = previousInstaller?.authenticatedActions || new Set();

function protectedLabel(label) {
  const normalized = String(label || "").toLowerCase().replace(/\s+/g, " ");
  return PROTECTED.some((term) => normalized.includes(term));
}

function manualOnlyLabel(label) {
  const normalized = String(label || "").toLowerCase().replace(/\s+/g, " ");
  return !normalized || MANUAL_ONLY.some((term) => normalized.includes(term));
}

function normaliseText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function usefulLabel(value) {
  const label = normaliseText(value);
  return Boolean(label) && label.length <= 180 && !/^WD[0-9A-F]+$/i.test(label) && !/^Unlabelled field$/i.test(label);
}

function textFromReferences(element) {
  const ids = (element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
  const text = ids.map((id) => document.getElementById(id)?.innerText || "").join(" ");
  return usefulLabel(text) ? normaliseText(text) : "";
}

function tableRowLabel(element) {
  const cell = element.closest("td, th");
  const row = cell?.closest("tr");
  if (!cell || !row) return "";
  const cells = Array.from(row.children).filter((child) => /^(TD|TH)$/.test(child.tagName));
  const index = cells.indexOf(cell);
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const text = normaliseText(cells[cursor].innerText);
    if (usefulLabel(text)) return text;
  }
  return "";
}

function nearbyLabel(element) {
  let current = element;
  for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
    let sibling = current.previousElementSibling;
    for (let count = 0; sibling && count < 4; count += 1, sibling = sibling.previousElementSibling) {
      const text = normaliseText(sibling.innerText);
      if (usefulLabel(text)) return text;
    }
  }
  return "";
}

function labelFor(element) {
  const labels = Array.from(element.labels || []).map((label) => label.innerText.trim()).filter(Boolean);
  const direct = labels.join(" ") || element.getAttribute("aria-label") || textFromReferences(element);
  if (usefulLabel(direct)) return normaliseText(direct);
  // React upload widgets commonly hide an anonymous input inside a labelled
  // container instead of using an associated HTML label.
  if (element.type === "file") {
    for (let parent = element.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth += 1) {
      if (parent.querySelectorAll('input[type="file"]').length !== 1) break;
      const text = normaliseText(parent.innerText || parent.textContent);
      if (usefulLabel(text) && /\b(r[e\u00e9]sum[e\u00e9]|cv|curriculum vitae|cover letter|attachment)(?=\W|$)/i.test(text)) return text;
    }
  }
  const inferred = tableRowLabel(element) || nearbyLabel(element);
  if (inferred) return inferred;
  return element.name || element.id || element.placeholder || "Unlabelled field";
}

function selectorFor(element) {
  if (element.id && document.querySelectorAll(`#${CSS.escape(element.id)}`).length === 1) return `#${CSS.escape(element.id)}`;
  const automationId = element.getAttribute("data-automation-id");
  if (automationId) {
    const selector = `${element.tagName.toLowerCase()}[data-automation-id="${CSS.escape(automationId)}"]`;
    if (document.querySelectorAll(selector).length === 1) return selector;
  }
  if (element.name) {
    const selector = `${element.tagName.toLowerCase()}[name="${CSS.escape(element.name)}"]`;
    if (document.querySelectorAll(selector).length === 1) return selector;
  }
  if (element.type === "file") {
    if (!element.hasAttribute("data-copilot-field")) element.setAttribute("data-copilot-field", `upload-${Date.now()}-${++generatedSelector}`);
    return `input[data-copilot-field="${CSS.escape(element.getAttribute("data-copilot-field"))}"]`;
  }
  return "";
}

function normaliseActionLabel(label) {
  return String(label || "").trim().replace(/\s+/g, " ").toLowerCase();
}

function actionLabelFor(element) {
  return normaliseText(
    element.getAttribute("aria-label") || textFromReferences(element)
    || (element.tagName === 'INPUT' ? element.value : '')
    || element.innerText || element.textContent || element.getAttribute("title") || ""
  );
}

const ACTION_CANDIDATES = "button, a, [role=button], input[type=button], input[type=submit], [onclick], [tabindex]";

function visible(element) {
  return element.getClientRects().length > 0
    && !element.disabled
    && element.getAttribute("aria-disabled") !== "true"
    && element.getAttribute("aria-hidden") !== "true"
    && getComputedStyle(element).visibility !== "hidden";
}

function clickableElement(element) {
  if (element.matches("button, a, [role=button], input[type=button], input[type=submit], [onclick]")) return element;
  return element.closest("button, a, [role=button], [onclick]") || element;
}

function actionCandidates() {
  const seen = new Set();
  return Array.from(document.querySelectorAll(ACTION_CANDIDATES))
    .filter((element) => !element.matches("input:not([type=button]):not([type=submit]), select, textarea"))
    .map(clickableElement)
    .filter((element) => visible(element) && !seen.has(element) && (seen.add(element), true))
    .filter((element) => {
      // The broad SAP/Web Dynpro fallback only admits elements whose own
      // visible label is one of the bounded, non-final actions below.
      const standard = element.matches("button, a, [role=button], input[type=button], input[type=submit], [onclick]");
      return standard || SAFE_CLICK.test(actionLabelFor(element));
    });
}

function staysInCurrentTab(element) {
  if (ENTRY_ACTION.test(actionLabelFor(element)) && element.matches('a[href]')
    && /^https?:/.test(element.href) && element.href.split('#')[0] !== location.href.split('#')[0]) return true;
  const owner = element.closest("a, button, input, form") || element;
  const target = owner.getAttribute("formtarget") || owner.target || owner.form?.target || "";
  return !target || target.toLowerCase() === "_self";
}

function safeActionSelector(label, occurrence) {
  return `safe-action:${normaliseActionLabel(label)}:${occurrence}`;
}

function actionElementFor(selector, label, prefix = "safe-action") {
  const match = String(selector || "").match(/^(safe-action|final-action|access-action|registration-action):(.*):(\d+)$/);
  if (!match || match[1] !== prefix || match[2] !== normaliseActionLabel(label)) return null;
  const expected = normaliseActionLabel(label);
  const matches = actionCandidates()
    .filter((element) => staysInCurrentTab(element) && normaliseActionLabel(actionLabelFor(element)) === expected);
  return matches[Number(match[3])] || null;
}

function documentKey(fields, actions, activeStep) {
  const fieldShape = fields.map((field) => `${field.selector}|${field.label}|${field.type}|${field.required}|${field.options.join("\u001d")}|${field.accept || ""}|${field.multiple || false}`).join("\u001f");
  const actionShape = actions.map((action) => `${action.selector}|${action.label}`).join("\u001f");
  return `${location.href}\u001e${activeStep}\u001e${fieldShape}\u001e${actionShape}`.slice(0, 45000);
}

function fileRole(label) {
  if (/\bcover\s*letter\b/i.test(label)) return "cover_letter";
  if (/\b(r[e\u00e9]sum[e\u00e9]|cv|curriculum vitae)(?=\W|$)/i.test(label)) return "resume";
  return "attachment";
}

function loginContext(button, registration = false) {
  if (!(registration ? REGISTER_ACTION : ACCESS_ACTION).test(actionLabelFor(button))) return null;
  let scope = button.form || button.closest("form");
  if (!scope) {
    for (let parent = button.parentElement, depth = 0; parent && parent !== document.body && depth < 6; parent = parent.parentElement, depth += 1) {
      if (Array.from(parent.querySelectorAll('input[type="password"]')).some(visible)) { scope = parent; break; }
    }
  }
  if (!scope) return null;
  const controls = Array.from(scope.querySelectorAll("input, select, textarea")).filter(visible);
  const passwords = controls.filter((element) => element.type === "password");
  const usernames = controls.filter((element) => /^(email|text)$/.test(element.type)
    && (/^(username|email)$/.test(element.autocomplete) || /\b(e-?mail|user\s*name)\b/i.test(labelFor(element))));
  if (!passwords.length || !usernames.length) return null;
  const challengeFrames = Array.from(scope.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"]')).filter(visible);
  const challengeTokens = Array.from(scope.querySelectorAll('[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'));
  const challengePending = (challengeFrames.length > 0 || challengeTokens.length > 0)
    && !challengeTokens.some((field) => Boolean(field.value));
  const unrelatedApplication = Array.from(document.querySelectorAll("input, select, textarea"))
    .some((element) => !scope.contains(element) && (visible(element) || element.type === "file")
      && !["button", "submit", "hidden", "password", "search"].includes(element.type)
      && !protectedLabel(labelFor(element)));
  return {
    button, scope, passwords, usernames, credentials: registration ? passwords : [...passwords, ...usernames], active: !unrelatedApplication,
    ready: passwords.every((element) => Boolean(element.value)) && usernames.some((element) => Boolean(element.value))
      && !challengePending
      && passwords.every((element) => (element.minLength < 0 || element.value.length >= element.minLength)
        && (element.maxLength < 0 || element.value.length <= element.maxLength))
      && (!registration || passwords.every((element) => element.value === passwords[0].value))
      && (!registration || controls.every((element) => element.type !== "checkbox"
        || !/\b(agree|terms|consent|privacy)\b/i.test(labelFor(element)) || element.checked))
      && controls.every((element) => !element.required || !element.validity || element.validity.valid),
  };
}

function accessState(candidates) {
  const contexts = candidates.map((button) => loginContext(button)).filter(Boolean);
  const credentials = new Set(contexts.flatMap((context) => context.credentials));
  const active = contexts.filter((context) => context.active);
  const applicationControls = Array.from(document.querySelectorAll("input, select, textarea"))
    .filter((element) => visible(element) && !credentials.has(element) && !protectedLabel(labelFor(element)));
  const pendingRequiredSecret = Array.from(document.querySelectorAll("input"))
    .some((element) => visible(element) && element.required && !element.value
      && (element.type === "password" || /\b(otp|verification code|one[- ]time|passcode)\b/i.test(labelFor(element)))
      && (!element.form || !applicationControls.length || applicationControls.some((control) => control.form === element.form)));
  return {
    contexts, credentials,
    pending: pendingRequiredSecret || active.some((context) => !context.ready),
    ready: active.some((context) => context.ready),
  };
}

function submissionConfirmed() {
  const confirmation = /^(?:(?:thank you|thanks)[!. ,]*(?:for (?:applying|your (?:application|interest)))[!. ,]*)?(?:(?:your|the) application (?:has been |was |is )?(?:successfully )?(?:submitted|received|sent)\b|(?:we(?:'ve| have) received your application)\b|(?:thank you|thanks) for (?:applying|your application)\b)/i;
  return Array.from(document.querySelectorAll("h1, h2, h3, [role=status], [role=alert], .success, p"))
    .filter(visible)
    .map((element) => normaliseText(element.innerText || element.textContent))
    .some((text) => text.length <= 500 && confirmation.test(text));
}

function visibleErrors() {
  const selectors = "[role=alert], [aria-live], .error, .errors, .err, .message-error, [class*=error], [class*=Error]";
  const seen = new Set();
  return Array.from(document.querySelectorAll(selectors))
    .filter((element) => element.getClientRects().length > 0)
    .map((element) => ({
      element,
      text: normaliseText(element.innerText || element.getAttribute("aria-label") || "")
    }))
    .filter(({ element, text }) => {
      const explicitError = element.getAttribute("role") === "alert" || /(?:^|[-_\s])(error|errors|err)(?:$|[-_\s])/i.test(element.className || "");
      const meaningfulLiveError = element.hasAttribute("aria-live")
        && /\b(error|invalid|required|mandatory|must|failed|cannot|please enter)\b/i.test(text);
      return (explicitError || meaningfulLiveError) && text && text.length <= 1000;
    })
    .map(({ text }) => text)
    .filter((text) => !seen.has(text) && (seen.add(text), true))
    .slice(0, 20);
}

const HSCNI_STEP_HINTS = [
  ["national insurance no", "hscni:personal-data"],
  ["permanent residence", "hscni:personal-data"],
  ["work experience", "hscni:work-experience"],
  ["cover letter", "hscni:cover-letter"],
  ["questionnaire", "hscni:questionnaire"],
  ["residency/immigration", "hscni:residency-immigration"],
  ["professional registration", "hscni:professional-registration"],
  ["referee details", "hscni:referee-details"],
  ["complete application", "hscni:complete-application"],
];

function activeStep(body) {
  const fullText = normaliseText(body);
  const hscni = location.hostname.toLowerCase() === "jobs-app.hscni.net";
  const navigationMarker = fullText.match(/previous\s+step\s+save\s*(?:&|and)\s*next/i)
    || fullText.match(/save\s*(?:&|and)\s*next/i);
  const currentText = hscni && navigationMarker
    ? fullText.slice(navigationMarker.index + navigationMarker[0].length)
    : fullText;
  if (hscni) {
    const lower = currentText.toLowerCase();
    const matched = HSCNI_STEP_HINTS.find(([marker]) => lower.includes(marker));
    if (matched) return matched[1];
  }
  const heading = Array.from(document.querySelectorAll("h1, h2, h3, h4, [role=heading], legend"))
    .filter(visible)
    .map((element) => normaliseText(element.innerText || element.textContent || ""))
    .find((text) => text && text.length <= 180 && !/^(application wizard|job application)$/i.test(text));
  return heading || "";
}

function qualificationSection(element) {
  if (!location.hostname.endsWith('.myworkdayjobs.com')) return null;
  for (let parent = element; parent && parent !== document.body; parent = parent.parentElement) {
    const marker = parent.getAttribute('data-automation-id') || '';
    const heading = Array.from(parent.querySelectorAll('h2,h3,h4,legend,[role=heading]'))
      .map((node) => normaliseText(node.textContent).toLowerCase());
    for (const kind of ['education', 'certifications', 'employment']) {
      const title = kind === 'employment' ? 'work experience' : kind;
      if (marker.toLowerCase() === `${kind === 'employment' ? 'workexperience' : kind}section` || (heading.includes(title)
        && !heading.includes(kind === 'education' ? 'certifications' : 'education')
        && (kind === 'employment' || !heading.includes('work experience')))) return { kind, element: parent };
    }
  }
  return null;
}

function qualificationMeta(element) {
  const section = qualificationSection(element);
  if (!section) return {};
  const prefix = section.kind === 'employment' ? 'workExperience' : section.kind === 'education' ? 'education' : 'certifications?';
  let row = '';
  for (let node = element; node && section.element.contains(node); node = node.parentElement) {
    const match = String(node.id || '').match(new RegExp(`^(${prefix}-[^-]+)(?:--|$)`, 'i'));
    if (match) { row = match[1]; break; }
  }
  return { resumeSection: section.kind, resumeRow: row };
}

function qualificationDropdown(element) {
  if (!qualificationMeta(element).resumeRow || element.tagName === 'SELECT') return false;
  return element.getAttribute('role') === 'combobox' || element.hasAttribute('aria-haspopup')
    || Boolean(element.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="selectWidget"]'));
}

function dropdownValue(element) {
  const widget = element.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="selectWidget"]') || element.parentElement;
  const selected = widget?.querySelector('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"]');
  if (selected) return normaliseText(selected.innerText).replace(/\s*remove\s*$/i, '');
  if (element.tagName === 'BUTTON') {
    const text = normaliseText(element.innerText);
    return /^(select|select one|choose|search)/i.test(text) ? '' : text;
  }
  return element.dataset.copilotSelected === element.value ? element.value : '';
}

function dropdownOptions(element) {
  if (element.tagName === 'SELECT') return Array.from(element.options).map((option) => option.text.trim());
  const ids = (element.getAttribute('aria-controls') || element.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
  return ids.flatMap((id) => Array.from(document.getElementById(id)?.querySelectorAll('[role=option], [data-automation-id=promptOption]') || []))
    .filter(visible).map((option) => normaliseText(option.innerText));
}

function qualificationSections(candidates) {
  const sections = new Map();
  for (const heading of document.querySelectorAll('h2,h3,h4,legend,[role=heading]')) {
    const section = qualificationSection(heading);
    if (section && visible(heading)) sections.set(section.kind, section);
  }
  return Array.from(sections.values()).map(({kind, element}) => {
    const adds = candidates.filter((button) => element.contains(button) && /^(add|add another)$/i.test(actionLabelFor(button)));
    return {kind, add: adds.length === 1 ? {selector: selectorFor(adds[0]) || `resume-add:${kind}`, label: actionLabelFor(adds[0])} : null};
  });
}

function snapshot() {
  const body = (document.body?.innerText || "").slice(0, 50000);
  const candidates = actionCandidates().filter(staysInCurrentTab);
  const access = accessState(candidates);
  const registrationContexts = candidates.map((element) => loginContext(element, true)).filter(Boolean);
  const registrationPending = registrationContexts.some((context) => context.active);
  const fields = Array.from(document.querySelectorAll('input, select, textarea, button[aria-haspopup], [role="combobox"]'))
    .filter((element) => element.type !== "hidden" && (!["button", "submit", "reset"].includes(element.type) || qualificationDropdown(element)))
    .filter((element) => visible(element) || (element.type === "file" && !element.disabled && element.getAttribute("aria-disabled") !== "true"))
    .map((element) => {
      const label = labelFor(element);
      if (element.type === "password" || protectedLabel(label) || access.credentials.has(element)) return null;
      return {
        selector: selectorFor(element), label, type: qualificationDropdown(element) ? 'combobox' : element.type || element.tagName.toLowerCase(),
        ...qualificationMeta(element),
        required: Boolean(element.required || element.getAttribute("aria-required") === "true"),
        invalid: element.getAttribute("aria-invalid") === "true" || Boolean(element.required && element.validity && !element.validity.valid),
        value: qualificationDropdown(element) ? dropdownValue(element) : element.type === "file" ? Array.from(element.files || []).map((file) => file.name).join(", ")
          : /^(checkbox|radio)$/.test(element.type) && !element.checked ? "" : element.value || "",
        options: dropdownOptions(element),
        ...(element.type === "file" ? {
          accept: element.accept || "", multiple: Boolean(element.multiple), fileRole: fileRole(label),
          files: Array.from(element.files || []).map((file) => file.name),
        } : {}),
      };
    }).filter(Boolean);
  const counts = new Map();
  const allActions = candidates
    .map((element) => {
      const label = actionLabelFor(element);
      const key = normaliseActionLabel(label);
      const occurrence = counts.get(key) || 0;
      counts.set(key, occurrence + 1);
      return { label, selector: safeActionSelector(label, occurrence), element };
    });
  const entryDestinations = new Set();
  const actions = allActions.filter((action) => SAFE_CLICK.test(action.label) && !protectedLabel(action.label))
    .filter((action) => {
      if (!ENTRY_ACTION.test(action.label)) return true;
      if (action.element.form || action.element.closest('form')) return false;
      const destination = action.element.matches('a[href]') ? action.element.href : '';
      // A job-page Apply link is still navigation when the page also has a
      // newsletter, language picker, or search controls. Non-link buttons
      // need an empty form context because Apply can also mean final submit.
      const navigates = /^https?:/.test(destination) && destination.split('#')[0] !== location.href.split('#')[0];
      if (!navigates && fields.some((field) => field.type !== 'search' && !/\bsearch\b/i.test(field.label))) return false;
      if (!destination) return true;
      if (entryDestinations.has(destination)) return false;
      entryDestinations.add(destination);
      return true;
    })
    .map(({ label, selector }) => ({ label, selector }));
  const finalActions = allActions.filter((action) => FINAL_ACTION.test(action.label)
    && !Array.from((action.element.form || document).querySelectorAll('input[type="password"]')).some(visible))
    .map(({ label, selector }) => ({ label, selector: selector.replace(/^safe-action:/, "final-action:") }));
  const accessActions = allActions.filter((action) => access.contexts.some((context) => context.button === action.element && context.active && context.ready))
    .map(({ label, selector }) => ({ label, selector: selector.replace(/^safe-action:/, "access-action:") }));
  const registrationActions = allActions.filter((action) => REGISTER_ACTION.test(action.label))
    .flatMap(({ label, selector, element }) => {
      const context = registrationContexts.find((item) => item.button === element);
      if (context && !context.active) return [];
      const prepare = context && context.passwords.every((field) => !field.value);
      if (context && !context.ready && !prepare) return [];
      // A bare registration link is navigation only. Never treat a form's
      // unrecognised submit button as a registration link.
      if (!context && (!element.matches("a[href]") || (fields.length && !access.pending))) return [];
      return [{ label, selector: selector.replace(/^safe-action:/, "registration-action:"), phase: context ? (prepare ? "prepare" : "create") : "open" }];
    });
  const step = activeStep(body);
  const resumeSections = qualificationSections(candidates);
  return {
    bridgeProtocol: BRIDGE_PROTOCOL, title: document.title, url: location.href, body, activeStep: step,
    fields, actions, finalActions, accessActions, registrationActions, registrationPending, resumeSections,
    registrationEmail: registrationContexts.filter((context) => context.active).flatMap((context) => context.usernames.map((field) => field.value)).find(Boolean) || "",
    accessPending: access.pending, accessReady: access.ready,
    submissionConfirmation: submissionConfirmed(), errors: visibleErrors(),
    documentKey: documentKey(fields, [...actions, ...finalActions, ...accessActions, ...registrationActions,
      ...resumeSections.filter((section) => section.add).map((section) => section.add)], step),
  };
}

function uploadFile(element, command) {
  if (element.type !== "file" || element.disabled || element.getAttribute("aria-disabled") === "true"
    || fileRole(labelFor(element)) !== "resume") return "blocked";
  if (element.files?.length) return "already_attached";
  const file = command.file;
  if (!file || typeof file.name !== "string" || !file.name.trim() || file.name.length > 255
    || /[\\/\x00-\x1f]/.test(file.name) || typeof file.dataBase64 !== "string"
    || !file.dataBase64.length || file.dataBase64.length > Math.ceil(MAX_UPLOAD_BYTES / 3) * 4
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(file.dataBase64)) return "invalid_file";
  const mimeType = String(file.mimeType || "application/octet-stream").toLowerCase();
  const accept = String(element.accept || "").toLowerCase().split(",").map((item) => item.trim()).filter(Boolean);
  const permitted = !accept.length || accept.some((item) => item.startsWith(".") ? file.name.toLowerCase().endsWith(item)
    : item.endsWith("/*") ? mimeType.startsWith(item.slice(0, -1)) : mimeType === item);
  if (!permitted) return "file_type_not_accepted";
  try {
    const binary = atob(file.dataBase64);
    if (!binary.length || binary.length > MAX_UPLOAD_BYTES) return "invalid_file";
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], file.name, { type: mimeType }));
    element.files = transfer.files;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return "uploaded";
  } catch (_error) {
    return "upload_failed";
  }
}

async function chooseQualification(element, command) {
  const normalize = (value) => normaliseText(value).toLowerCase().replace(/[\u2018\u2019]/g, "'");
  const wanted = (command.aliases || [command.value]).map(normalize);
  if (element.tagName === 'SELECT') {
    const matches = Array.from(element.options).filter((option) => wanted.includes(normalize(option.text)));
    if (matches.length !== 1) return 'option_not_found';
    element.value = matches[0].value;
    element.dispatchEvent(new Event('change', {bubbles:true}));
    return 'applied';
  }
  try {
  if (element.getAttribute('aria-expanded') !== 'true') element.click();
  const typeQuery = (query) => {
    if (element.tagName !== 'INPUT' || element.readOnly) return;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, query);
    element.dispatchEvent(new Event('input', {bubbles:true}));
    element.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true}));
  };
  typeQuery(command.value);
  for (let attempt = 0; attempt < 15; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    const ids = (element.getAttribute('aria-controls') || element.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
    let lists = ids.map((id) => document.getElementById(id)).filter(Boolean);
    if (!lists.length) lists = Array.from(document.querySelectorAll('[role=listbox]')).filter(visible);
    if (lists.length !== 1) continue;
    const options = Array.from(lists[0].querySelectorAll('[role=option], [data-automation-id=promptOption]')).filter(visible);
    const matches = options.filter((option) => wanted.includes(normalize(option.innerText)));
    if (matches.length > 1) return 'option_not_found';
    if (matches.length === 1) {
      matches[0].click();
      await new Promise((resolve) => setTimeout(resolve, 150));
      if (element.tagName === 'INPUT' && element.value && element.getAttribute('aria-expanded') !== 'true') {
        element.dataset.copilotSelected = element.value;
      }
      return dropdownValue(element) ? 'applied' : 'unsupported';
    }
  }
  return 'option_not_found';
  } finally {
    // Do not leave failed menus stacked over the next field.
    element.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
    element.blur();
  }
}

async function execute(commands) {
  const results = [];
  for (const command of commands || []) {
    const current = snapshot();
    if (command.documentKey && current.documentKey !== command.documentKey) {
      results.push({ commandId: command.commandId, selector: command.selector, status: "stale_document" });
      continue;
    }
    if (command.kind === 'resume-add') {
      const section = current.resumeSections.find((item) => item.kind === command.section && item.add?.selector === command.selector && item.add?.label === command.label);
      const rows = new Set(current.fields.filter((field) => field.resumeSection === command.section).map((field) => field.resumeRow).filter(Boolean));
      let element = null;
      if (section && command.documentKey && command.authorized === true && rows.size === command.rowCount) {
        element = actionCandidates().find((button) => qualificationSection(button)?.kind === command.section
          && /^(add|add another)$/i.test(actionLabelFor(button))
          && (command.selector === `resume-add:${command.section}` || selectorFor(button) === command.selector));
      }
      if (element) element.click();
      results.push({commandId:command.commandId, selector:command.selector, status:element ? 'clicked' : 'blocked'});
      continue;
    }
    if (["upload", "submit", "authenticate", "register", "check"].includes(command.kind) && !command.documentKey) {
      results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
      continue;
    }
    if (command.kind === "register") {
      const action = current.registrationActions.find((item) => item.selector === command.selector && item.label === command.label);
      const element = actionElementFor(command.selector, command.label, "registration-action");
      if (command.authorized !== true || !action || !element || command.phase !== action.phase || current.errors.length
        || (action.phase === "create" && !loginContext(element, true)?.ready)) {
        results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
        continue;
      }
      if (action.phase === "prepare") {
        const context = loginContext(element, true);
        if (!context || !context.passwords.every((field) => !field.value)
          || !command.password || !context.usernames.some((field) => field.value === command.email)) {
          results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
          continue;
        }
        for (const field of context.passwords) {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, command.password);
          field.dispatchEvent(new Event("input", { bubbles: true }));
          field.dispatchEvent(new Event("change", { bubbles: true }));
        }
        results.push({ commandId: command.commandId, selector: command.selector, status: "applied" });
        continue;
      }
      const key = `${location.href}\u001f${command.selector}`;
      if (authenticatedActions.has(key)) {
        results.push({ commandId: command.commandId, selector: command.selector, status: "already_attempted" });
        continue;
      }
      authenticatedActions.add(key);
      element.click();
      results.push({ commandId: command.commandId, selector: command.selector, status: "clicked" });
      continue;
    }
    if (command.kind === "submit" || command.kind === "authenticate") {
      const submitting = command.kind === "submit";
      const allowed = submitting ? current.finalActions : current.accessActions;
      const element = actionElementFor(command.selector, command.label, submitting ? "final-action" : "access-action");
      const recognized = submitting ? FINAL_ACTION : ACCESS_ACTION;
      const matching = allowed.some((action) => action.selector === command.selector && action.label === command.label);
      if (command.authorized !== true || !matching || !element || !recognized.test(actionLabelFor(element))
        || !staysInCurrentTab(element) || (!submitting && !loginContext(element)?.ready)) {
        results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
        continue;
      }
      if (submitting && (current.errors.length || (element.form && !element.form.checkValidity()))) {
        results.push({ commandId: command.commandId, selector: command.selector, status: "validation_failed" });
        continue;
      }
      const attempts = submitting ? submittedActions : authenticatedActions;
      const attemptKey = `${location.href}\u001f${command.selector}`;
      if (attempts.has(attemptKey)) {
        results.push({ commandId: command.commandId, selector: command.selector, status: submitting ? "already_submitted" : "already_attempted" });
        continue;
      }
      // Record before dispatch: a synchronous handler or a lost bridge reply
      // must not turn a retried command into a duplicate application.
      attempts.add(attemptKey);
      element.click();
      results.push({ commandId: command.commandId, selector: command.selector, status: submitting ? "submission_clicked" : "clicked" });
      continue;
    }
    const isSafeNavigation = command.kind === "click" && SAFE_CLICK.test(command.label || "")
      && /^safe-action:.*:\d+$/.test(command.selector || "");
    let element;
    try { element = isSafeNavigation ? actionElementFor(command.selector, command.label) : document.querySelector(command.selector); }
    catch (_error) { element = null; }
    const label = command.label || (element ? labelFor(element) : "");
    if (!element || manualOnlyLabel(label) || manualOnlyLabel(isSafeNavigation ? actionLabelFor(element) : labelFor(element))
      || element.type === "password" || element.disabled) {
      results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
      continue;
    }
    if (command.kind === "upload") {
      const matching = current.fields.some((field) => field.selector === command.selector && field.label === command.label && field.type === "file");
      results.push({ commandId: command.commandId, selector: command.selector, status: matching ? uploadFile(element, command) : "blocked" });
      continue;
    }
    if (command.kind === 'resume-select') {
      const field = current.fields.find((item) => item.selector === command.selector && item.label === command.label && item.resumeRow);
      const status = command.documentKey && field && (qualificationDropdown(element) || element.tagName === 'SELECT')
        ? await chooseQualification(element, command) : 'blocked';
      results.push({commandId:command.commandId, selector:command.selector, status});
      continue;
    }
    const workdayDate = command.kind === 'fill' && location.hostname.endsWith('.myworkdayjobs.com')
      && element.tagName === 'INPUT' && /-dateSection(?:Month|Year|Day)-input$/.test(element.id);
    if (isSafeNavigation && staysInCurrentTab(element)) {
      if (ENTRY_ACTION.test(command.label)) {
        if (!current.actions.some((action) => action.selector === command.selector && action.label === command.label)) {
          results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
          continue;
        }
        if (element.matches('a[href]') && /^https?:/.test(element.href)) location.assign(element.href);
        else element.click();
      } else element.click();
      results.push({ commandId: command.commandId, selector: command.selector, status: "clicked" });
      continue;
    } else if (isSafeNavigation) {
      results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
      continue;
    } else if (command.kind === "check") {
      if (!location.hostname.endsWith('.myworkdayjobs.com') || element.type !== 'checkbox'
        || !/^workExperience-[\w-]+--currentlyWorkHere$/.test(element.id)
        || normaliseActionLabel(labelFor(element)) !== 'i currently work here'
        || !['true', 'false'].includes(command.value)) {
        results.push({ commandId: command.commandId, selector: command.selector, status: "blocked" });
        continue;
      }
      if (element.checked !== (command.value === 'true')) element.click();
    } else if (command.kind === "select" && element.tagName === "SELECT") {
      const option = Array.from(element.options).find((item) => item.value === command.value || item.text.trim() === command.value);
      if (!option) { results.push({ commandId: command.commandId, selector: command.selector, status: "option_not_found" }); continue; }
      element.value = option.value;
    } else if (command.kind === "fill" && /^(INPUT|TEXTAREA)$/.test(element.tagName)
      && SAFE_MEMORY_CONTROLS.has(String(element.type || element.tagName).toLowerCase())) {
      const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const set = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (!set) { results.push({ commandId: command.commandId, selector: command.selector, status: "unsupported" }); continue; }
      // Workday date segments commit through focus/focusout. A dispatched
      // blur event alone neither focuses the input nor triggers React's
      // delegated focusout handler, leaving visible text uncommitted.
      if (workdayDate) element.focus();
      set.call(element, command.value);
    } else {
      results.push({ commandId: command.commandId, selector: command.selector, status: "unsupported" });
      continue;
    }
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    if (workdayDate) {
      element.blur();
      await new Promise((resolve) => setTimeout(resolve, 0));
    } else element.dispatchEvent(new Event("blur", { bubbles: true }));
    results.push({ commandId: command.commandId, selector: command.selector, status: "applied" });
  }
  return results;
}

const messageListener = (message, _sender, sendResponse) => {
  if (disposed) return;
  if (message.type === "application-snapshot") sendResponse(snapshot());
  if (message.type === "application-commands") {
    execute(message.commands).then(sendResponse).catch(() => sendResponse([]));
    return true;
  }
};
chrome.runtime.onMessage.addListener(messageListener);

// Many ATS pages replace form sections without a full navigation. Notify the
// paired-tab background worker after a short quiet period so Autopilot can
// inspect the next section without the candidate clicking Sync again.
let snapshotTimer;
let lastSnapshotSignal = 0;
const notifyDomChange = () => {
  if (disposed) return;
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(() => {
    if (disposed) return;
    if (Date.now() - lastSnapshotSignal < 1000) return;
    lastSnapshotSignal = Date.now();
    chrome.runtime.sendMessage({ type: "application-dom-changed" }).catch(() => {});
  }, 900);
};
const observer = new MutationObserver(notifyDomChange);
observer.observe(document.documentElement, {
  childList: true, subtree: true, characterData: true,
});

// Input values do not necessarily create DOM mutations.  SuccessFactors and
// other ATS pages can therefore remain on an old snapshot after the candidate
// completes a login or corrects a validation error.  Treat form activity as a
// debounced refresh signal. Password controls are still excluded by snapshot()
// itself, so this never reads or sends their values.
const formActivityListener = (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const relevant = target.matches("input, select, textarea, form, button, [role=button]")
    || Boolean(target.closest("form, button, [role=button]"));
  if (relevant) notifyDomChange();
};
document.addEventListener("input", formActivityListener, true);
document.addEventListener("change", formActivityListener, true);
document.addEventListener("submit", formActivityListener, true);
document.addEventListener("click", formActivityListener, true);

// Learn only edits actually typed or selected by the candidate.  The agent's
// own fill commands dispatch synthetic events (`isTrusted === false`), so it
// cannot mistakenly teach itself an output and replay it as a future answer.
const manualLearningTimers = new Map();
const learnedManualValues = new Map();
const learnManualField = (event) => {
  if (!event.isTrusted) return;
  const element = event.target;
  if (!(element instanceof HTMLInputElement) && !(element instanceof HTMLTextAreaElement)) return;
  if (accessState(actionCandidates()).credentials.has(element)) return;
  const control = String(element.type || element.tagName || "").toLowerCase();
  const label = labelFor(element);
  const selector = selectorFor(element);
  const value = String(element.value || "").trim();
  if (!selector || !value || value.length > 10000 || !SAFE_MEMORY_CONTROLS.has(control) || manualOnlyLabel(label)) return;
  if (/^WD[0-9A-F]+$/i.test(normaliseText(label)) || /^Unlabelled field$/i.test(normaliseText(label))) return;
  const field = { selector, label, type: control, value };
  clearTimeout(manualLearningTimers.get(selector));
  manualLearningTimers.set(selector, setTimeout(() => {
    manualLearningTimers.delete(selector);
    if (learnedManualValues.get(selector) === field.value) return;
    learnedManualValues.set(selector, field.value);
    chrome.runtime.sendMessage({ type: "application-manual-field", field }).catch(() => {});
  }, 700));
};
document.addEventListener("input", learnManualField, true);

// The dashboard can queue a command after the tab was paired.  A small local
// heartbeat wakes the service worker promptly, instead of making the candidate
// wait for Chrome's 30-second minimum alarm interval.  It is ignored unless
// this exact tab is the explicitly paired application tab.
const heartbeatTimer = setInterval(() => {
  if (disposed) return;
  chrome.runtime.sendMessage({ type: "application-autopilot-poll" }).catch(() => {});
}, 4000);

globalThis[INSTALL_KEY] = {
  build: CONTENT_BUILD,
  submittedActions,
  authenticatedActions,
  dispose() {
    disposed = true;
    clearTimeout(snapshotTimer);
    clearInterval(heartbeatTimer);
    observer.disconnect();
    document.removeEventListener("input", formActivityListener, true);
    document.removeEventListener("change", formActivityListener, true);
    document.removeEventListener("submit", formActivityListener, true);
    document.removeEventListener("click", formActivityListener, true);
    document.removeEventListener("input", learnManualField, true);
    for (const timer of manualLearningTimers.values()) clearTimeout(timer);
    manualLearningTimers.clear();
    learnedManualValues.clear();
    chrome.runtime.onMessage.removeListener(messageListener);
  },
};
})();
