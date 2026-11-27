// node capture.mjs <baseUrl> <outDir>
// Drives the Sentinel demo in headless Chrome and saves the screens the explainer video needs.
import { spawn } from "node:child_process";
import fs from "node:fs";
const [base, out] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const p = spawn(chrome, ["--headless=new", "--disable-gpu", "--remote-debugging-port=9556", `--user-data-dir=${out}/../.cdp`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let list;
for (let i = 0; i < 40; i++) { try { list = await (await fetch("http://127.0.0.1:9556/json/list")).json(); break; } catch { await sleep(250); } }
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.onopen = r);
let n = 0; const wait = new Map();
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && wait.has(d.id)) { wait.get(d.id)(d); wait.delete(d.id); } };
const send = (method, params = {}) => new Promise(res => { const id = ++n; wait.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
const ev = async e => (await send("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result?.result?.value;
const post = (path, body, role = "planner") => fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json", "X-Role": role }, body: JSON.stringify(body || {}) });
async function shot(name, selector) {
  if (name !== "approved") await ev(`document.querySelector('#toast').classList.remove('on')`);
  await sleep(350);
  let clip;
  if (selector) {
    const r = await ev(`(() => { scrollTo(0, 0); const e = document.querySelector(${JSON.stringify(selector)}); const b = e.getBoundingClientRect(); return {x: b.x - 16, y: b.y - 16, width: b.width + 32, height: Math.min(b.height + 32, 1100)}; })()`);
    await sleep(300);
    clip = { ...r, scale: 1 };
  }
  const res = await send("Page.captureScreenshot", { format: "png", ...(clip ? { clip, captureBeyondViewport: true } : {}) });
  fs.writeFileSync(`${out}/${name}.png`, Buffer.from(res.result.data, "base64"));
  await ev("scrollTo(0,0)");
}
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 860, deviceScaleFactor: 1.5, mobile: false });

await post("/api/reset", { seed: 7 });
await send("Page.navigate", { url: base + "/" }); await sleep(3000);
await shot("calm");
await post("/api/advance", { days: 9.5 }); await post("/api/inject/supplier_delay"); await post("/api/advance", { days: 0.25 });
await ev("refresh()"); await sleep(1500);
await shot("late");
await shot("runway", ".runway");
await ev(`document.querySelector('.rw-row.late').click()`); await sleep(1500);
await shot("explain");
await ev(`closeSheet()`); await sleep(500);
await shot("decision", ".decision");
await ev(`document.querySelector('[data-role=manager]').click()`); await sleep(1200);
await shot("manager", ".decision");
await ev(`document.querySelector('[data-role=planner]').click()`); await sleep(1200);
await ev(`document.querySelector('[data-ap]').click()`); await sleep(2000);
await shot("approved");
await post("/api/inject/outage"); await post("/api/advance", { days: 1 });
await ev("refresh()"); await sleep(1500);
await shot("outage");
await ev(`document.querySelector('#evBtn').click()`); await sleep(1500);
await shot("evidence");
await ev(`closeSheet()`); await sleep(400);
// phone
await post("/api/reset", { seed: 7 }); await post("/api/advance", { days: 9.5 }); await post("/api/inject/supplier_delay"); await post("/api/advance", { days: 0.25 });
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await send("Page.reload"); await sleep(3000);
await shot("phone-today");
await ev(`document.querySelector('[data-go=orders]').click()`); await sleep(800);
await shot("phone-orders");
await post("/api/reset", { seed: 7 });
console.log("captured", fs.readdirSync(out).length, "files");
ws.close(); p.kill(); process.exit(0);
