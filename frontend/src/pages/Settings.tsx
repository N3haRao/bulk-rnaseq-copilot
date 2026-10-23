import { useEffect, useState, type FormEvent } from "react";
import { api, type KeyStatus } from "../api";
import { Notice } from "../components/common";

export default function Settings() {
  const [status, setStatus] = useState<KeyStatus | null>(null);
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "pass" | "warning" | "fail"; text: string } | null>(null);

  useEffect(() => { api.settings().then((s) => setStatus(s.anthropic)).catch(() => undefined); }, []);

  async function save(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const r = await api.saveKey(key);
      setStatus(r.anthropic);
      setKey("");
      setMessage({ kind: r.message.startsWith("The key works") ? "pass" : "warning", text: r.message });
    } catch (e) {
      setMessage({ kind: "fail", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm("Remove the saved API key? The chat will stop working until you add one again.")) return;
    try {
      const r = await api.deleteKey();
      setStatus(r.anthropic);
      setMessage({ kind: "pass", text: "The key was removed from this computer." });
    } catch (e) {
      setMessage({ kind: "fail", text: (e as Error).message });
    }
  }

  return (
    <>
      <h1>Settings</h1>
      <section className="card">
        <h2>Anthropic API key (for the chat)</h2>
        <p className="muted">
          The Ask (chat) tab uses Claude through the Anthropic API. Create a key at{" "}
          <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a>{" "}
          (Settings → API keys) and paste it here. We recommend a key used only for this site, with a monthly spending
          limit set in the Anthropic console.
        </p>

        {status && (
          status.configured ? (
            <Notice kind="pass">
              A key is set: <code>{status.masked}</code>, stored in {status.storage}. The chat uses {status.model}.
            </Notice>
          ) : <Notice kind="warning">No key yet: the chat is switched off until you add one.</Notice>
        )}

        {status && !status.can_change ? (
          <p className="muted small">This key comes from the ANTHROPIC_API_KEY setting (.env file or environment) and takes
            precedence. To manage the key here instead, remove that line from .env and restart the site.</p>
        ) : (
          <form onSubmit={save} autoComplete="off">
            <label className="field">
              <span>{status?.configured ? "Replace the key" : "API key"}</span>
              <div className="row" style={{ gap: 8 }}>
                <input type={show ? "text" : "password"} value={key} onChange={(e) => setKey(e.target.value)}
                  placeholder="sk-ant-…" autoComplete="off" spellCheck={false} name="anthropic-api-key"
                  style={{ flex: "1 1 320px", maxWidth: 460, fontFamily: "ui-monospace, Menlo, monospace" }} aria-label="Anthropic API key" />
                <button type="button" className="secondary" onClick={() => setShow(!show)}>{show ? "Hide" : "Show"}</button>
              </div>
              <small>It is checked with Anthropic, then saved on this computer. It is never shown again in full.</small>
            </label>
            <div className="row">
              <button type="submit" disabled={busy || !key.trim()}>
                {busy ? <><span className="spinner" /> Checking…</> : "Check and save"}
              </button>
              {status?.configured && <button type="button" className="secondary" onClick={remove}>Remove key</button>}
            </div>
          </form>
        )}
        {message && <Notice kind={message.kind}>{message.text}</Notice>}

        <details style={{ marginTop: 12 }}>
          <summary className="small">How the key is kept safe</summary>
          <ul className="small">
            <li>It is stored in your computer's keychain (on a Mac, the macOS Keychain), or where there is none, in a file only
              your user account can read.</li>
            <li>The site never sends it back to the browser; it only shows the first and last characters.</li>
            <li>Only this computer, and only this site's own pages, can change or remove it.</li>
            <li>The code the assistant writes runs without the key in its environment and without keychain access.</li>
            <li>Questions and results you send in the chat go to Anthropic; use public data during the trial.</li>
          </ul>
        </details>
      </section>
    </>
  );
}
