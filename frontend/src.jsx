import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "https://bulk-mail-automation.onrender.com").replace(/\/$/, "");
const API = `${API_BASE}/api`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Render free instances sleep when idle; retry GET requests while the backend wakes up (~30-60s).
async function request(path, options = {}, retries = 0) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(API + path, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map(d => d.msg || "Invalid input").join("; ") : data.detail;
        const err = new Error(detail || `Request failed (${res.status})`);
        err.status = res.status;
        err.code = data.code;
        err.retryable = data.retryable;
        err.retryAfter = data.retry_after;
        throw err;
      }
      return data;
    } catch (e) {
      lastErr = e;
      const retryable = !e.status || e.status === 502 || e.status === 503 || e.status === 504;
      if (!retryable || attempt === retries) break;
      await sleep(4000);
    }
  }
  throw lastErr;
}
const ALLOWED_EXT = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "png", "jpg", "jpeg", "gif", "webp"];
const fmtSize = (b) => (b >= 1048576 ? (b / 1048576).toFixed(2) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");
const fileToBase64 = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
  reader.onerror = () => reject(new Error("Could not read " + file.name));
  reader.readAsDataURL(file);
});
const defaultInstruction = "Write a concise, professional recruitment outreach email. Do not invent facts, claims, or promises. Use {{name}}, {{company}}, and {{role}} placeholders where suitable.";

// Convert API values to renderable text; React cannot render objects such as {name: "..."} directly.
function safeText(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(safeText).filter(Boolean).join(", ");
  if (typeof value === "object") {
    if (value.name != null) return safeText(value.name);
    if (value.email != null) return safeText(value.email);
    return "";
  }
  return "";
}

function App() {
  const [health, setHealth] = useState(null);
  const [contacts, setContacts] = useState([]);
  const [stats, setStats] = useState({ contacts: 0, consented_contacts: 0, drafts: {} });
  const [instruction, setInstruction] = useState(defaultInstruction);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(null);
  const [files, setFiles] = useState([]);
  const [mode, setMode] = useState("ai");          // "ai" = generate with Gemini, "manual" = type your own template
  const [aiError, setAiError] = useState(null);    // { message, retryable, retryAfter }
  const sendLock = useRef(false);                  // blocks double-click / duplicate campaigns

  async function refresh() {
    try {
      const [h, c, s] = await Promise.all([request("/health", {}, 12), request("/contacts", {}, 12), request("/stats", {}, 12)]);
      setHealth(h); setContacts(Array.isArray(c) ? c : []); setStats(s && typeof s === "object" ? s : { contacts: 0, consented_contacts: 0, drafts: {} });
    } catch (e) { setError("Cannot reach backend (Render may still be waking up, retry in a minute). " + e.message); }
  }
  useEffect(() => { refresh(); }, []);
  function showError(e) { setError(e.message || String(e)); setMessage(""); window.scrollTo?.({ top: 0, behavior: "smooth" }); }

  const maxFiles = health?.max_attachments || 5;
  const maxBytes = (health?.max_attachment_mb || 3) * 1024 * 1024;
  function addFiles(e) {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";
    if (!picked.length) return;
    setError(""); setMessage("");
    const next = [...files];
    for (const f of picked) {
      const ext = (f.name.split(".").pop() || "").toLowerCase();
      if (!ALLOWED_EXT.includes(ext)) { setError(`".${ext}" file allowed nahi hai. Allowed: ${ALLOWED_EXT.join(", ")}`); return; }
      if (next.some(x => x.name === f.name && x.size === f.size)) continue;
      next.push(f);
    }
    if (next.length > maxFiles) { setError(`Maximum ${maxFiles} files allowed.`); return; }
    const total = next.reduce((a, f) => a + f.size, 0);
    if (total > maxBytes) { setError(`Total attachment size ${fmtSize(total)} hai. Limit ${fmtSize(maxBytes)} hai.`); return; }
    setFiles(next);
  }
  function removeFile(index) { setFiles(files.filter((_, i) => i !== index)); }

  async function upload(e) {
    const file = e.target.files?.[0]; if (!file) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const form = new FormData(); form.append("file", file);
      const result = await request("/contacts/upload", { method: "POST", body: form });
      setMessage(`CSV imported: ${result.added} added, ${result.updated || 0} updated, ${result.skipped} skipped.`);
      await refresh();
    } catch (err) { showError(err); } finally { setBusy(false); e.target.value = ""; }
  }
  async function setConsent(contact, approved) {
    setBusy(true); setError(""); setMessage("");
    try {
      await request(`/contacts/${contact.id}/consent`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved }) });
      setMessage(approved ? `Marked ${safeText(contact.email)} as authorized.` : `Removed authorization for ${safeText(contact.email)}.`);
      await refresh();
    } catch (err) { showError(err); } finally { setBusy(false); }
  }
  async function generateTemplate() {
    if (busy) return;
    setBusy(true); setError(""); setMessage(""); setAiError(null);
    try {
      const result = await request("/template/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction }) });
      setSubject(result.subject); setBody(result.body);   // only overwritten on success; typed text is kept on failure
      setMessage(`One reusable template generated${result.model ? ` (${result.model})` : ""}. Review and edit it before sending.`);
    } catch (err) {
      const retryable = err.retryable !== undefined ? err.retryable : !err.status;   // network errors are retryable
      setAiError({ message: err.message, retryable, retryAfter: err.retryAfter });
    } finally { setBusy(false); }
  }
  async function sendCampaign() {
    if (sendLock.current) return;
    if (!subject.trim() || !body.trim()) { showError(new Error("Subject and email body are required. Type them yourself (Manual Template) or generate them with AI.")); return; }
    // Fresh check from the backend: the page may be stale if the Render free instance restarted (its SQLite data resets).
    let authorized = stats.consented_contacts;
    try {
      const fresh = await request("/stats");
      authorized = fresh.consented_contacts || 0;
      if (authorized !== stats.consented_contacts) await refresh();
    } catch (err) { showError(err); return; }
    if (!authorized) { showError(new Error("Backend me koi authorized contact nahi hai. Render free plan restart par data reset ho jata hai. CSV dobara upload karo aur 'I have permission' lagao.")); return; }
    const confirmed = window.confirm(`Start one bulk campaign for ${authorized} authorized contact(s)?\n\nGemini will NOT generate a separate draft per contact. The same template will be personalized with {{name}}, {{company}}, {{role}} and sent in batches.${files.length ? `\n\nAttachments (${files.length}): ${files.map(f => f.name).join(", ")}` : ""}\n\nContinue only if these recipients are authorized and your Brevo plan supports the volume.`);
    if (!confirmed) return;
    sendLock.current = true;
    const requestId = (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`).slice(0, 64);
    setBusy(true); setError(""); setMessage(""); setProgress({ sent: 0, failed: 0, pending: authorized, total: authorized });
    try {
      const attachments = await Promise.all(files.map(async f => ({ name: f.name, content: await fileToBase64(f) })));
      const campaign = await request("/campaigns", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subject, body, attachments, request_id: requestId }) });
      let latest = { sent: 0, failed: 0, pending: campaign.total, total: campaign.total };
      let firstError = "";
      setProgress(latest);
      // Process one small batch per request so a single long HTTP request doesn't handle all 5,000 recipients.
      while (true) {
        const batch = await request(`/campaigns/${campaign.campaign_id}/process`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit: campaign.batch_size || 20 }) });
        latest = { sent: batch.sent || 0, failed: batch.failed || 0, pending: batch.pending || 0, total: campaign.total };
        setProgress(latest);
        if (!firstError && batch.errors?.length) firstError = `${batch.errors[0].email}: ${batch.errors[0].error}`;
        if (batch.done) break;
        if (!batch.processed && latest.pending > 0) throw new Error("Campaign made no progress. Check Render logs and retry after resolving the error.");
      }
      setMessage(`Campaign finished. Accepted by Brevo: ${latest.sent}; failed: ${latest.failed}. Check the campaign results before retrying failed recipients.`);
      if (latest.failed > 0 && firstError) showError(new Error(`Email fail hua. Reason: ${firstError}`));
      await refresh();
    } catch (err) { showError(err); }
    finally { setBusy(false); sendLock.current = false; }
  }

  const consented = contacts.filter(c => c.consent).length;
  const statusClass = s => "status " + (s || "draft").replace(/\s/g, "-");
  return <div className="app">
    <header>
      <div><div className="eyebrow">AI + EMAIL WORKFLOW</div><h1>AI Email Automation</h1><p>Generate one reusable email template and send it to authorized contacts in batches.</p></div>
      <button className="secondary" onClick={refresh} disabled={busy}>Refresh</button>
    </header>
    {error && <div className="alert error"><b>Problem:</b> {error}<button onClick={() => setError("")}>×</button></div>}
    {message && <div className="alert success"><span style={{whiteSpace:"pre-wrap"}}>{message}</span><button onClick={() => setMessage("")}>×</button></div>}
    <section className="cards">
      <div className="metric"><span>Contacts</span><strong>{stats.contacts}</strong></div>
      <div className="metric"><span>Authorized contacts</span><strong>{consented}</strong></div>
      <div className="metric"><span>Existing drafts</span><strong>{Object.values(stats.drafts || {}).reduce((a, b) => a + b, 0)}</strong></div>
      <div className="metric"><span>Previously sent drafts</span><strong>{stats.drafts?.sent || 0}</strong></div>
    </section>
    <section className="panel">
      <div className="section-head"><div><h2>1. Import contacts</h2><p>CSV headers: email, consent, name, company, role, context</p></div><label className="upload">Choose CSV<input type="file" accept=".csv,text/csv" onChange={upload} disabled={busy}/></label></div>
      <div className="notice">Only mark permission for contacts you are genuinely authorized to email. Uploading a contact alone is not consent. The bulk campaign sends only to contacts marked as authorized.</div>
      <div className="table-wrap"><table><thead><tr><th>Email</th><th>Name</th><th>Company</th><th>Permission</th><th>Authorize</th></tr></thead><tbody>
        {contacts.map(c => <tr key={safeText(c.id)}><td>{safeText(c.email)}</td><td>{safeText(c.name)}</td><td>{safeText(c.company)}</td><td><span className={statusClass(c.consent ? "consented" : "not-consented")}>{c.consent ? "yes" : "no"}</span></td><td><button className={c.consent ? "secondary" : ""} disabled={busy} onClick={() => setConsent(c, !c.consent)}>{c.consent ? "Remove" : "I have permission"}</button></td></tr>)}
        {!contacts.length && <tr><td colSpan="5" className="empty">No contacts yet. Upload a CSV to get started.</td></tr>}
      </tbody></table></div>
    </section>
    <section className="panel">
      <h2>2. Create one email template</h2>
      <div className="mode-toggle">
        <button className={mode === "ai" ? "" : "secondary"} onClick={() => { setMode("ai"); setAiError(null); }} disabled={busy}>Generate with AI</button>
        <button className={mode === "manual" ? "" : "secondary"} onClick={() => { setMode("manual"); setAiError(null); }} disabled={busy}>Use Manual Template</button>
      </div>
      {mode === "ai" ? <>
        <p>Gemini is called once to create a reusable template, not once per contact. You can edit the subject and body before sending.</p>
        <label className="field-label">Instructions to AI</label><textarea rows="3" value={instruction} onChange={e => setInstruction(e.target.value)} disabled={busy}/>
        <div className="action-row"><span>Placeholders: <code>{"{{name}}"}</code> <code>{"{{email}}"}</code> <code>{"{{company}}"}</code> <code>{"{{role}}"}</code> <code>{"{{context}}"}</code></span><button onClick={generateTemplate} disabled={busy}>{busy ? "Working…" : "Generate one template"}</button></div>
        {aiError && <div className="alert error ai-error">
          <div><b>AI template could not be generated.</b> {aiError.message}{aiError.retryAfter ? ` (try again in about ${aiError.retryAfter}s)` : ""}<br/>Your typed subject and body are kept. You can send a campaign without AI.</div>
          <div className="ai-error-actions">
            {aiError.retryable && <button onClick={generateTemplate} disabled={busy}>Retry</button>}
            <button className="secondary" onClick={() => { setMode("manual"); setAiError(null); }} disabled={busy}>Use Manual Template</button>
          </div>
        </div>}
      </> : <>
        <p>Manual mode: Gemini is not used. Type your own subject and body below. Placeholders are filled in for each contact when sending: <code>{"{{name}}"}</code> <code>{"{{email}}"}</code> <code>{"{{company}}"}</code> <code>{"{{role}}"}</code> <code>{"{{context}}"}</code></p>
      </>}
      <label className="field-label">Email subject</label><input value={subject} onChange={e => setSubject(e.target.value)} maxLength="250" disabled={busy}/>
      <label className="field-label">Email body</label><textarea rows="10" value={body} onChange={e => setBody(e.target.value)} maxLength="12000" disabled={busy} placeholder="Your reusable email template will appear here…"/>
      <label className="field-label">Attachments (optional, photo ya file)</label>
      <div className="attach-box">
        <label className="upload attach-btn">Add files<input type="file" multiple accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.png,.jpg,.jpeg,.gif,.webp" onChange={addFiles} disabled={busy}/></label>
        <span className="attach-hint">Max {maxFiles} files, total {fmtSize(maxBytes)}. Ye files har recipient ko jayengi.</span>
      </div>
      {files.length > 0 && <ul className="attach-list">
        {files.map((f, i) => <li key={f.name + f.size}><span>📎 {f.name} <em>({fmtSize(f.size)})</em></span><button className="secondary" onClick={() => removeFile(i)} disabled={busy}>Remove</button></li>)}
        <li className="attach-total">Total: {fmtSize(files.reduce((a, f) => a + f.size, 0))}</li>
      </ul>}
      <div className="notice">Example: “Hi {"{{name}}"}” will be personalized for each recipient. Do not add claims or facts that you cannot verify.</div>
    </section>
    <section className="panel">
      <h2>3. Send campaign in batches</h2>
      <p>Only authorized contacts are queued. Each request processes up to {Math.min(health?.max_send_per_request || 20, 20)} emails to avoid one giant request. Sending 5,000 emails still depends on your provider's daily quota and account limits.</p>
      {error && <div className="alert error"><span><b>Problem:</b> {error}</span><button onClick={() => setError("")}>×</button></div>}
      <button onClick={sendCampaign} disabled={busy || !subject.trim() || !body.trim() || !consented}>{busy ? "Campaign processing…" : `Send to ${consented} authorized contact(s)`}</button>
      {progress && <div style={{marginTop:16}}><strong>Campaign progress</strong><p>Sent: {progress.sent} · Failed: {progress.failed} · Pending: {progress.pending} / {progress.total}</p><div style={{height:10,background:"#e5e7eb",borderRadius:99,overflow:"hidden"}}><div style={{height:"100%",width:`${progress.total ? ((progress.sent + progress.failed) / progress.total) * 100 : 0}%`,background:"#2563eb",transition:"width .2s"}}/></div></div>}
      <div className="notice">Failed emails are recorded in the campaign queue and are not automatically retried, to avoid duplicate sends. “Accepted by Brevo” does not guarantee inbox delivery.</div>
    </section>
    <footer><span>Backend: {health ? "connected" : "checking…"}</span><span>Gemini: {health?.gemini_configured ? "configured" : "not configured"}</span><span>Brevo: {health?.brevo_configured ? "configured" : "not configured"}</span></footer>
  </div>;
}
createRoot(document.getElementById("root")).render(<App/>);
