// node render.mjs <jobs.json>   jobs = [{html, png}] ; renders each HTML file to a 1920x1080 PNG
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
const jobs = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const prof = path.join(path.dirname(process.argv[2]), ".cdp-render");
const p = spawn(chrome, ["--headless=new", "--disable-gpu", "--remote-debugging-port=9557", "--allow-file-access-from-files", `--user-data-dir=${prof}`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let list;
for (let i = 0; i < 40; i++) { try { list = await (await fetch("http://127.0.0.1:9557/json/list")).json(); break; } catch { await sleep(250); } }
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.onopen = r);
let n = 0; const wait = new Map(); const events = [];
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && wait.has(d.id)) { wait.get(d.id)(d); wait.delete(d.id); } else events.push(d); };
const send = (method, params = {}) => new Promise(res => { const id = ++n; wait.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
for (const j of jobs) {
  events.length = 0;
  await send("Page.navigate", { url: "file:///" + j.html.replace(/\\/g, "/") });
  for (let i = 0; i < 100 && !events.some(e => e.method === "Page.loadEventFired"); i++) await sleep(50);
  await send("Runtime.evaluate", { expression: "document.fonts.ready.then(() => new Promise(r => setTimeout(r, 120)))", awaitPromise: true });
  const r = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(j.png, Buffer.from(r.result.data, "base64"));
}
console.log("rendered", jobs.length);
ws.close(); p.kill(); process.exit(0);
