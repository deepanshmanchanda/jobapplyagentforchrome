const BRIDGE = "http://127.0.0.1:8765";
const BRIDGE_PROTOCOL = 8;
const COMMAND_RESULTS_KEY = "applicationBridgeCommandResults";
const MANAGED_TAB_IDS_KEY = "managedApplicationTabIds";
const MANAGED_TAB_IDS_MIGRATED_KEY = "managedApplicationTabsMigrated";
const LEGACY_PAIRED_TAB_ID_KEY = "pairedTabId";
const NAVIGATION_GUARD_KEY = "applicationBridgeNavigationGuards";
const commandStorage = chrome.storage.session || chrome.storage.local;
const managedStorage = chrome.storage.session || chrome.storage.local;
const pollInFlightTabIds = new Set();
const navigationInFlightTabIds = new Set();
const navigationInFlightTimers = new Map();
const bootstrapErrorsByTab = new Map();
// Storage updates are read-modify-write.  Serialize those mutations inside a
// worker so rapidly starting two different application tabs cannot cause one
// tab ID to overwrite the other in the managed list.
let managedTabMutation = Promise.resolve();

// Inject while Chrome's action-click ``activeTab`` grant is live, then open
// the side panel. This avoids requiring the candidate to change global Chrome
// site-access settings merely because a previously open ATS tab needs the
// content bridge reattached after an extension reload.
chrome.action?.onClicked?.addListener(async (tab) => {
  if (!tab?.id || !/^https?:/.test(tab.url || "")) return;
  let injection;
  try {
    // Start the injection first, then open the panel without awaiting either
    // operation. `sidePanel.open()` still runs in the direct user-gesture
    // turn, while `activeTab` authorises this same current application tab.
    injection = chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: false }, files: ["content.js"], injectImmediately: true,
    });
  } catch (error) {
    bootstrapErrorsByTab.set(
      String(tab.id),
      `Chrome could not attach to this page: ${error.message || error}`,
    );
  }
  const panelOpening = chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
  if (injection) {
    try {
      await injection;
      bootstrapErrorsByTab.delete(String(tab.id));
    } catch (error) {
      bootstrapErrorsByTab.set(
        String(tab.id),
        `Chrome could not attach to this page: ${error.message || error}`,
      );
    }
  }
  try {
    await panelOpening;
  } catch (_error) {
    // The candidate can still open the side panel from Chrome's side-panel menu.
  }
});

function isMissingReceiver(error) {
  return /receiving end does not exist|could not establish connection|message port closed/i.test(String(error?.message || error));
}

function isHostPermissionError(error) {
  return /cannot access contents of url|manifest must request permission|missing host permission/i.test(String(error?.message || error));
}

function hostPatternFor(tab) {
  const url = new URL(tab.url || "");
  if (!/^https?:$/.test(url.protocol)) throw new Error("Open a normal HTTP(S) application tab first.");
  return `${url.protocol}//${url.hostname}/*`;
}

async function requestHostAccess(tab) {
  const origin = hostPatternFor(tab);
  const host = new URL(tab.url).hostname;
  if (!chrome.permissions?.contains || !chrome.permissions?.request) {
    throw new Error(`Allow Application Autopilot to access ${host}, then try this page again.`);
  }
  if (await chrome.permissions.contains({ origins: [origin] })) return;
  try {
    if (await chrome.permissions.request({ origins: [origin] })) return;
  } catch (_error) {
    // Chrome may require its newer per-tab access affordance instead of a
    // modal prompt when the user has restricted site access.
  }
  if (chrome.permissions.addHostAccessRequest) {
    try {
      await chrome.permissions.addHostAccessRequest({ tabId: tab.id });
    } catch (_error) {
      // The concise error below explains the remaining user action.
    }
  }
  throw new Error(`Allow Application Autopilot to access ${host} in Chrome, then choose Start Autopilot again.`);
}

async function wait(milliseconds) {
  if (milliseconds) await new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function sameTab(left, right) {
  return String(left) === String(right);
}

function uniqueTabIds(values) {
  return (Array.isArray(values) ? values : []).filter((value, index, source) =>
    value !== undefined && value !== null && source.findIndex((other) => sameTab(other, value)) === index
  );
}

function commandResultsKey(tabId) {
  return `${COMMAND_RESULTS_KEY}:${String(tabId)}`;
}

function navigationGuardKey(tabId) {
  return `${NAVIGATION_GUARD_KEY}:${String(tabId)}`;
}

function updateManagedTabs(operation) {
  const task = managedTabMutation.then(operation, operation);
  // Keep the queue usable after a failed storage operation, while returning
  // the original failure to the caller that initiated it.
  managedTabMutation = task.catch(() => {});
  return task;
}

async function managedTabIds() {
  const stored = await managedStorage.get(MANAGED_TAB_IDS_KEY);
  if (Array.isArray(stored[MANAGED_TAB_IDS_KEY])) return uniqueTabIds(stored[MANAGED_TAB_IDS_KEY]);

  // One-time migration from the pre-multi-tab local pointer. New managed tab
  // IDs live in session storage, so a browser restart cannot accidentally
  // reuse a stale numeric Chrome tab ID.
  if (managedStorage !== chrome.storage.local) {
    const legacy = await chrome.storage.local.get([
      MANAGED_TAB_IDS_KEY, LEGACY_PAIRED_TAB_ID_KEY, MANAGED_TAB_IDS_MIGRATED_KEY,
    ]);
    if (!legacy[MANAGED_TAB_IDS_MIGRATED_KEY]) {
      const ids = uniqueTabIds([
        ...(Array.isArray(legacy[MANAGED_TAB_IDS_KEY]) ? legacy[MANAGED_TAB_IDS_KEY] : []),
        legacy[LEGACY_PAIRED_TAB_ID_KEY],
      ]);
      await managedStorage.set({ [MANAGED_TAB_IDS_KEY]: ids });
      await chrome.storage.local.set({ [MANAGED_TAB_IDS_MIGRATED_KEY]: true });
      await chrome.storage.local.remove(MANAGED_TAB_IDS_KEY);
      return ids;
    }
    return [];
  }

  // Fallback for older Chrome versions without storage.session.
  const legacy = await chrome.storage.local.get(LEGACY_PAIRED_TAB_ID_KEY);
  return uniqueTabIds([legacy[LEGACY_PAIRED_TAB_ID_KEY]]);
}

async function isManagedTab(tabId) {
  return (await managedTabIds()).some((candidate) => sameTab(candidate, tabId));
}

async function addManagedTab(tabId) {
  return updateManagedTabs(async () => {
    const ids = await managedTabIds();
    if (!ids.some((candidate) => sameTab(candidate, tabId))) ids.push(tabId);
    // pairedTabId remains a compatibility pointer for the old diagnostics view;
    // the managed list is authoritative for the side panel and background loop.
    await managedStorage.set({ [MANAGED_TAB_IDS_KEY]: ids });
    await chrome.storage.local.set({ [LEGACY_PAIRED_TAB_ID_KEY]: tabId });
    return ids;
  });
}

async function removeManagedTab(tabId) {
  return updateManagedTabs(async () => {
    const ids = (await managedTabIds()).filter((candidate) => !sameTab(candidate, tabId));
    const stored = await chrome.storage.local.get(LEGACY_PAIRED_TAB_ID_KEY);
    const next = {};
    if (sameTab(stored[LEGACY_PAIRED_TAB_ID_KEY], tabId)) {
      if (ids.length) next[LEGACY_PAIRED_TAB_ID_KEY] = ids[ids.length - 1];
      else next[LEGACY_PAIRED_TAB_ID_KEY] = null;
    }
    await managedStorage.set({ [MANAGED_TAB_IDS_KEY]: ids });
    if (Object.keys(next).length) await chrome.storage.local.set(next);
    if (!ids.length) await chrome.storage.local.remove(LEGACY_PAIRED_TAB_ID_KEY);
    return ids;
  });
}

async function probeSnapshot(tabId) {
  const snapshot = await chrome.tabs.sendMessage(tabId, { type: "application-snapshot" });
  if (!snapshot || typeof snapshot !== "object" || !snapshot.url) {
    throw new Error("The application page did not return a usable form snapshot.");
  }
  return snapshot;
}

async function requestSnapshot(tab, { allowInjection = false } = {}) {
  const tabId = tab.id;
  try {
    return await probeSnapshot(tabId);
  } catch (error) {
    // An unpacked extension reload does not always attach its declared content
    // script to an already-open ATS tab.  Recover during the explicit Pair
    // action, without making the candidate reload or leave that page.
    if (!allowInjection || !isMissingReceiver(error)) throw error;
    const inject = () => chrome.scripting.executeScript({
      target: { tabId, allFrames: false }, files: ["content.js"], injectImmediately: true,
    });
    try {
      await inject();
    } catch (injectionError) {
      if (!isHostPermissionError(injectionError)) throw injectionError;
      // A candidate may have manually restricted this extension's requested
      // HTTP(S) access in Chrome. Ask for this current employer host again,
      // rather than asking the candidate to hunt through Chrome settings.
      await requestHostAccess(tab);
      await inject();
    }
    let latest = error;
    for (const delay of [0, 100, 300]) {
      await wait(delay);
      try {
        return await probeSnapshot(tabId);
      } catch (retryError) {
        latest = retryError;
        if (!isMissingReceiver(retryError)) throw retryError;
      }
    }
    throw latest;
  }
}

async function sendSnapshot(tab, markManaged = false, allowInjection = false) {
  const snapshot = await requestSnapshot(tab, { allowInjection });
  const response = await fetch(`${BRIDGE}/v1/tabs/snapshot`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...snapshot, bridgeProtocol: BRIDGE_PROTOCOL, paired: markManaged,
      tabId: String(tab.id), url: tab.url || snapshot.url, title: tab.title || snapshot.title,
    })
  });
  if (!response.ok) {
    let detail = `Local bridge returned ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (_error) {
      // A non-JSON local failure still has the concise status above.
    }
    throw new Error(detail);
  }
  if (markManaged) await addManagedTab(tab.id);
  return snapshot;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id || !/^https?:/.test(tab.url || "")) throw new Error("Open a normal HTTP(S) application tab first.");
  return tab;
}

async function managedActiveTab() {
  const tab = await activeTab();
  if (!await isManagedTab(tab.id)) throw new Error("Start Autopilot on this application tab first.");
  return tab;
}

async function bridgeJSON(path, options = {}) {
  const response = await fetch(`${BRIDGE}${path}`, options);
  if (!response.ok) {
    let detail = `Local bridge returned ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (_error) {
      // Preserve the concise status when a local process returns non-JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

async function panelState() {
  try {
    const [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!activeTab?.id || !/^https?:/.test(activeTab.url || "")) {
      return { paired: false, managedCount: (await managedTabIds()).length, message: "Open an HTTP(S) application page and choose Start Autopilot." };
    }
    const ids = await managedTabIds();
    const currentIsManaged = ids.some((id) => sameTab(id, activeTab.id));
    const currentTab = { id: activeTab.id, title: activeTab.title || "Current application page", url: activeTab.url || "" };
    const bootstrapError = bootstrapErrorsByTab.get(String(activeTab.id));
    if (bootstrapError) {
      return { paired: false, tab: currentTab, managedCount: ids.length, error: bootstrapError };
    }
    if (!currentIsManaged) {
      return {
        paired: false, tab: currentTab, managedCount: ids.length,
        message: ids.length
          ? `${ids.length} other application${ids.length === 1 ? " is" : "s are"} already running. Start Autopilot here to add this page.`
          : "Open an application page and choose Start Autopilot.",
      };
    }
    const tabs = await bridgeJSON("/v1/tabs");
    const snapshot = (tabs.tabs || []).find((item) => sameTab(item.tabId, activeTab.id));
    const autopilot = snapshot
      ? await bridgeJSON(`/v1/autopilot/${activeTab.id}`)
      : { status: { state: "waiting_for_snapshot", reason: "Waiting for this page to connect to the local bridge." } };
    return {
      paired: true,
      isCurrentTab: true,
      managedCount: ids.length,
      tab: { ...currentTab, title: activeTab.title || snapshot?.title || "Application page" },
      snapshot: snapshot ? {
        step: snapshot.activeStep || "Current application step",
        fieldCount: (snapshot.fields || []).length,
        errors: snapshot.errors || [],
      } : null,
      autopilot: autopilot.status,
    };
  } catch (error) {
    return {
      paired: false,
      managedCount: (await managedTabIds()).length,
      error: `Local agent is unavailable: ${error.message}`,
    };
  }
}

async function startAutopilotForActiveTab({ autoSubmit = true, autoRegister = true } = {}) {
  const tab = await activeTab();
  // This is an explicit button press on the current page. It adds this page
  // to the managed set without stopping other independently started runs.
  // Every command remains keyed to its own browser tab ID.
  await sendSnapshot(tab, true, true);
  const payload = await bridgeJSON(`/v1/autopilot/${tab.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // The extension intentionally sends no candidate data.  The loopback
    // bridge reads the user's saved local profile/answers itself.
    body: JSON.stringify({ maxSteps: 25, autoSubmit: autoSubmit !== false, autoRegister: autoRegister !== false }),
  });
  await pollManagedTab(tab.id);
  return payload.status;
}

async function stopAutopilotForActiveTab() {
  const tab = await managedActiveTab();
  const payload = await bridgeJSON(`/v1/autopilot/${tab.id}`, { method: "DELETE" });
  return payload.status;
}

async function instructAutopilotForActiveTab(instruction, remember) {
  const tab = await managedActiveTab();
  // Refresh the current page shape before matching an exact visible label.
  // This keeps a global side panel bound to the tab the candidate is actually
  // looking at, never an older application tab.
  await sendSnapshot(tab, false, true);
  const payload = await bridgeJSON(`/v1/autopilot/${tab.id}/instruction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction: String(instruction || ""), remember: Boolean(remember) }),
  });
  await pollManagedTab(tab.id);
  return payload;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if ![
    'pair-active-tab', 'sync-active-tab',
    'panel-status', 'panel-start-autopilot', 'panel-stop-autopilot', 'panel-instruction-autopilot',
  ].includes(message.type)) return;
  (async () => {
    try {
      if (message.type === 'panel-status') {
        sendResponse({ ok: true, panel: await panelState() });
        return;
      }
      if (message.type === 'panel-start-autopilot') {
        const status = await startAutopilotForActiveTab(message);
        sendResponse({
          ok: true,
          message: 'Autopilot started in this tab.',
          status,
          panel: await panelState(),
        });
        return;
      }
      if (message.type === 'panel-stop-autopilot') {
        const status = await stopAutopilotForActiveTab();
        sendResponse({
          ok: true,
          message: 'Autopilot stopped for this application tab.',
          status,
          panel: await panelState(),
        });
        return;
      }
      if (message.type === 'panel-instruction-autopilot') {
        const result = await instructAutopilotForActiveTab(message.instruction, message.remember);
        sendResponse({
          ok: true,
          message: result.message || 'Instruction accepted. Rechecking the current page.',
          status: result.status,
          panel: await panelState(),
        });
        return;
      }
      const isPair = message.type === "pair-active-tab";
      const tab = isPair ? await activeTab() : await managedActiveTab();
      await sendSnapshot(tab, isPair, isPair);
      if (!isPair && message.type === "sync-active-tab") {
        await pollManagedTab(tab.id);
      }
      sendResponse({
        ok: true,
        message: isPair
          ? "Active tab paired. Open Application Autopilot and select Start Autopilot."
          : "Paired application tab synced. An active run continues in this same tab.",
      });
    } catch (error) {
      sendResponse({ ok: false, message: `Could not use this application tab: ${error.message}` });
    }
  })();
  return true;
});

// Content scripts report SPA/Web Dynpro DOM replacements. Each explicitly
// started tab is processed independently, so several application pages can be
// prepared in parallel without crossing tab IDs.
chrome.runtime.onMessage.addListener((message, sender) => {
  if (!['application-dom-changed', 'application-autopilot-poll', 'application-manual-field'].includes(message.type) || !sender.tab?.id) return;
  (async () => {
    if (!await isManagedTab(sender.tab.id) || !/^https?:/.test(sender.tab.url || "")) return;
    try {
      if (message.type === "application-manual-field") {
        // A content script emits these only for trusted candidate typing.
        // Refresh first so the bridge can prove the selector/label belongs to
        // this currently paired page before retaining a site-scoped answer.
        await sendSnapshot(sender.tab);
        await bridgeJSON(`/v1/autopilot/${sender.tab.id}/learned-field`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ field: message.field || {} }),
        });
        return;
      }
      if (message.type === "application-dom-changed") await sendSnapshot(sender.tab);
      await pollManagedTab(sender.tab.id);
    } catch (_error) {
      // The periodic local-only poll retries; no page contents leave this device.
    }
  })();
});

function commandReceipt(result) {
  // Receipt caching prevents replay after a worker restart. Retain only the
  // acknowledgement identifiers: commands may carry resume bytes, and a
  // future credential command must never persist its payload in Chrome.
  return {
    commandId: result.commandId,
    selector: result.selector,
    status: result.status,
  };
}

function commandMayNavigate(command) {
  return ["click", "submit", "authenticate"].includes(command.kind)
    || (command.kind === "register" && command.phase !== "prepare");
}

async function commandResultsOnce(tabId, commands) {
  const storageKey = commandResultsKey(tabId);
  const stored = (await commandStorage.get(storageKey))[storageKey] || {};
  const cached = [];
  const fresh = [];
  for (const command of commands) {
    if (command.commandId && stored[command.commandId]) cached.push(commandReceipt(stored[command.commandId]));
    else fresh.push(command);
  }
  if (!fresh.length) return { results: cached, executedClick: false };
  const rawResults = await chrome.tabs.sendMessage(tabId, { type: "application-commands", commands: fresh });
  const applied = (rawResults || []).map(commandReceipt);
  const nextStored = Object.fromEntries(Object.entries(stored).map(([id, result]) => [id, commandReceipt(result)]));
  for (const result of applied) {
    if (result.commandId) nextStored[result.commandId] = result;
  }
  await commandStorage.set({ [storageKey]: nextStored });
  return {
    results: [...cached, ...applied],
    // A content script can suppress a repeated submit/sign-in attempt.
    // Retain the navigation guard only when it actually dispatched a click.
    executedClick: applied.some((result) => ["clicked", "submission_clicked"].includes(result.status)),
  };
}

async function clearDeliveredCommandResults(tabId, results) {
  const storageKey = commandResultsKey(tabId);
  const stored = (await commandStorage.get(storageKey))[storageKey] || {};
  let changed = false;
  for (const result of results) {
    if (result.commandId && stored[result.commandId]) {
      delete stored[result.commandId];
      changed = true;
    }
  }
  if (changed) await commandStorage.set({ [storageKey]: stored });
}

async function armSameTabNavigationGuard(tabId) {
  const key = String(tabId);
  navigationInFlightTabIds.add(key);
  clearTimeout(navigationInFlightTimers.get(key));
  navigationInFlightTimers.set(key, setTimeout(() => { void clearSameTabNavigationGuard(tabId); }, 15000));
  // MV3 workers can be suspended between a click and a newly-created tab.
  // Persist an independent, non-sensitive guard for each managed tab.
  await commandStorage.set({ [navigationGuardKey(tabId)]: Date.now() + 15000 });
}

async function clearSameTabNavigationGuard(tabId) {
  const key = String(tabId);
  navigationInFlightTabIds.delete(key);
  clearTimeout(navigationInFlightTimers.get(key));
  navigationInFlightTimers.delete(key);
  await commandStorage.remove(navigationGuardKey(tabId));
}

async function hasSameTabNavigationGuard(tabId) {
  const key = String(tabId);
  if (navigationInFlightTabIds.has(key)) return true;
  const storageKey = navigationGuardKey(tabId);
  const expiresAt = (await commandStorage.get(storageKey))[storageKey];
  const now = Date.now();
  if (!Number.isFinite(expiresAt) || expiresAt <= now) {
    await commandStorage.remove(storageKey);
    return false;
  }
  return true;
}

async function pollManagedTab(tabId) {
  const key = String(tabId);
  if (pollInFlightTabIds.has(key)) return;
  pollInFlightTabIds.add(key);
  let followUp = false;
  try {
    const response = await fetch(`${BRIDGE}/v1/commands/${tabId}`);
    if (!response.ok) throw new Error(`Local bridge returned ${response.status}`);
    const { commands } = await response.json();
    if (!commands.length) return;
    // Arm this before DOM click dispatch.  Some sites create a new tab
    // synchronously, so a result-based guard would race that event.
    const hasSafeNavigation = commands.some(commandMayNavigate);
    if (hasSafeNavigation) await armSameTabNavigationGuard(tabId);
    const { results, executedClick } = await commandResultsOnce(tabId, commands);
    if (hasSafeNavigation && !executedClick) await clearSameTabNavigationGuard(tabId);
    const resultResponse = await fetch(`${BRIDGE}/v1/results`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tabId: String(tabId), results }) });
    if (!resultResponse.ok) throw new Error(`Local bridge did not accept command results (${resultResponse.status})`);
    await clearDeliveredCommandResults(tabId, results);
    const tab = await chrome.tabs.get(tabId);
    await sendSnapshot(tab);
    followUp = !executedClick;
  } catch (_error) {
    // Commands remain queued until a positive browser acknowledgement. The next
    // poll retries; no credentials or errors are sent off-device.
  } finally {
    pollInFlightTabIds.delete(key);
    // A field batch can queue the next batch immediately. Do not wait for
    // the 30-second recovery alarm when input events were coalesced.
    if (followUp) setTimeout(() => { void pollManagedTab(tabId); }, 150);
  }
}

chrome.alarms.create("application-bridge-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "application-bridge-poll") return;
  (async () => {
    // The alarm is a recovery heartbeat for a page that did not emit a DOM or
    // load event.  It lets the bridge enforce its navigation timeout instead
    // of leaving an Autopilot run waiting forever.
    for (const tabId of await managedTabIds()) {
      try {
        await sendSnapshot(await chrome.tabs.get(tabId));
      } catch (_error) {
        // A closed or reloaded page is removed from the managed set below, or
        // recovered by its next explicit Start.
      }
      await pollManagedTab(tabId);
    }
  })();
});

// A safe navigation step such as "Apply Manually" changes the page before it
// exposes fields. Refresh the paired snapshot when that navigation completes.
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete" || !/^https?:/.test(tab.url || "")) return;
  if (!await isManagedTab(tabId)) return;
  try {
    await sendSnapshot(tab);
    await clearSameTabNavigationGuard(tabId);
    await pollManagedTab(tabId);
  } catch (_error) {
    // The popup's Sync action and the next alarm poll safely retry on dynamic pages.
  }
});

// A recognised safe action must remain in the paired application tab. If a
// page script tries to open a new tab while that action is in flight, close it.
chrome.tabs.onCreated.addListener(async (tab) => {
  if (!tab.id || tab.openerTabId === undefined || tab.openerTabId === null) return;
  if (!await hasSameTabNavigationGuard(tab.openerTabId)) return;
  try {
    await chrome.tabs.remove(tab.id);
  } catch (_error) {
    // The paired tab is still left untouched; the bridge will time out and pause.
  }
});

chrome.tabs.onRemoved?.addListener(async (tabId) => {
  if (!await isManagedTab(tabId)) return;
  await removeManagedTab(tabId);
  await clearSameTabNavigationGuard(tabId);
  try {
    await bridgeJSON(`/v1/autopilot/${tabId}`, { method: "DELETE" });
  } catch (_error) {
    // The local bridge may already have been restarted; stale state cannot
    // reach a closed tab in either case.
  }
});
