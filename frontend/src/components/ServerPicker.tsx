import { useEffect, useState } from "react";
import { api, type ServerListing } from "../api";
import { Notice } from "./common";

// Browse the folders the server allows (data/incoming, the FASTQ jobs, the references, SERVER_FILE_ROOTS)
// and choose files or folders for one input. A choice is "<root>:<path>"; the server checks it again and
// links the file into the analysis instead of copying it.

export type PickMode = "file" | "files" | "folder";

const fmtSize = (b: number | null) =>
  b === null ? "" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`;

export function pickLabel(token: string): string {
  const [root, ...rest] = token.split(":");
  return `${root}: ${rest.join(":") || "/"}`;
}

export default function ServerPicker({ mode, accept, onPick, onClose }: {
  mode: PickMode; accept: string[]; onPick: (tokens: string[]) => void; onClose: () => void;
}) {
  const [where, setWhere] = useState<{ root?: string; path: string }>({ path: "" });
  const [list, setList] = useState<ServerListing | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  // .zip is a "file" only for the upload field; on the server a folder of sample folders is the natural choice
  const fileAccept = mode === "folder" ? [".zip"] : accept;

  useEffect(() => {
    let live = true;
    setErr(null);
    api.serverFiles(where.root, where.path, fileAccept)
      .then((l) => { if (live) setList(l); })
      .catch((e) => { if (live) setErr((e as Error).message); });
    return () => { live = false; };
  }, [where.root, where.path]); // eslint-disable-line react-hooks/exhaustive-deps

  const token = (path: string) => `${list?.root}:${path}`;
  const toggle = (t: string) => setChosen((cur) =>
    mode === "files" ? (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]) : (cur[0] === t ? [] : [t]));
  const folderHelp = mode === "folder" ? "Choose the folder that holds one folder per sample (e.g. the FASTQ job's results/salmon)."
    : mode === "files" ? "Tick files, or use a whole folder: the files in it (up to two levels down) that fit this input are used."
    : "Choose one file.";

  return (
    <div className="server-picker">
      <div className="row">
        <strong>Choose on the server</strong>
        <span className="spacer" />
        <button type="button" className="link" onClick={onClose}>Close</button>
      </div>
      <p className="muted small" style={{ margin: "4px 0 8px" }}>{folderHelp} Files are linked, not copied.</p>
      {list && (
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          {list.roots.map((r) => (
            <button type="button" key={r.key} className={list.root === r.key ? "" : "secondary"} title={r.path}
              onClick={() => { setChosen([]); setWhere({ root: r.key, path: "" }); }}>{r.label}</button>
          ))}
        </div>
      )}
      {err && <Notice kind="fail">{err}</Notice>}
      {list?.root && (
        <>
          <div className="row small" style={{ margin: "8px 0", flexWrap: "wrap" }}>
            <button type="button" className="link" onClick={() => setWhere({ root: list.root!, path: "" })}>{list.root_label}</button>
            {list.path.split("/").filter(Boolean).map((part, i, all) => (
              <span key={i}> / <button type="button" className="link"
                onClick={() => setWhere({ root: list.root!, path: all.slice(0, i + 1).join("/") })}>{part}</button></span>
            ))}
            <span className="spacer" />
            {mode !== "file" && list.path && (
              <button type="button" className="secondary" onClick={() => onPick([token(list.path)])}>Use this folder</button>
            )}
          </div>
          <div className="table-wrap server-list">
            <table>
              <tbody>
                {list.parent !== null && list.parent !== undefined && (
                  <tr><td colSpan={3}><button type="button" className="link" onClick={() => setWhere({ root: list.root!, path: list.parent! })}>↑ up</button></td></tr>
                )}
                {list.entries.map((e) => {
                  const t = token(e.path);
                  const pickable = !e.dir || mode !== "file";
                  return (
                    <tr key={e.path}>
                      <td style={{ width: 28 }}>
                        {pickable && <input type={mode === "files" ? "checkbox" : "radio"} aria-label={`choose ${e.name}`}
                          checked={chosen.includes(t)} onChange={() => toggle(t)} />}
                      </td>
                      <td>
                        {e.dir
                          ? <button type="button" className="link" onClick={() => setWhere({ root: list.root!, path: e.path })}>📁 {e.name}</button>
                          : <span>{e.name}</span>}
                        {e.note && <span className="muted small"> — {e.note}</span>}
                      </td>
                      <td className="muted small" style={{ textAlign: "right", whiteSpace: "nowrap" }}>{fmtSize(e.size)}</td>
                    </tr>
                  );
                })}
                {!list.entries.length && <tr><td colSpan={3} className="muted small">Nothing here that fits this input.</td></tr>}
              </tbody>
            </table>
          </div>
          {list.truncated && <p className="muted small">Only the first 2000 entries are shown.</p>}
          <div className="row" style={{ marginTop: 8 }}>
            <button type="button" disabled={!chosen.length} onClick={() => onPick(chosen)}>
              Use {chosen.length > 1 ? `these ${chosen.length}` : "the chosen one"}
            </button>
          </div>
        </>
      )}
      {list && !list.roots.length && <p className="muted small">No server folders are set up for browsing.</p>}
    </div>
  );
}
