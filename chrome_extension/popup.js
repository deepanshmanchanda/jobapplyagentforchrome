const status = document.getElementById("status");

async function send(type) {
  const result = await chrome.runtime.sendMessage({ type });
  status.textContent = result?.message || "The local bridge did not respond.";
}

document.getElementById("pair").addEventListener("click", () => send("pair-active-tab"));
document.getElementById("sync").addEventListener("click", () => send("sync-active-tab"));
