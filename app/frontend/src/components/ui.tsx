import React from "react";
import type { DisplayStatus } from "../api";

export const STATUS_TEXT: Record<string, string> = {
  VERIFIED: "verified",
  UNVERIFIED_FORMULA: "withheld: formula not verified",
  UNAPPROVED_CALIBRATION: "withheld: calibration not approved",
  QC_BAD: "withheld: QC flag BAD",
  MISSING: "withheld: reading missing",
};

export function fmt(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}

export function utc(ts?: string | null): string {
  if (!ts) return "—";
  return ts.replace("T", " ").replace("Z", " UTC");
}

export function StatusBadge({ status }: { status?: DisplayStatus | string | null }) {
  if (!status) return null;
  const cls = status === "VERIFIED" ? "badge ok" : "badge warn";
  return <span className={cls} title={STATUS_TEXT[status] || status}>{status === "VERIFIED" ? "✓ verified" : `⚠ ${STATUS_TEXT[status] || status}`}</span>;
}

export function QcBadge({ flag, reason }: { flag?: string | null; reason?: string | null }) {
  if (!flag) return <span className="badge muted">QC —</span>;
  const cls = flag === "OK" ? "badge ok" : flag === "SUSPECT" ? "badge warn" : "badge bad";
  return <span className={cls} title={reason || ""}>QC {flag}{reason ? ` (${reason})` : ""}</span>;
}

/** A computed value with its unit, calibration version and QC flag. Withheld values never show a number. */
export function Value(props: {
  value?: number | null;
  unit?: string | null;
  status?: string | null;
  calVersion?: number | string | null;
  qa?: string | null;
  qaReason?: string | null;
  digits?: number;
}) {
  const shown = props.status === "VERIFIED" && props.value !== null && props.value !== undefined;
  return (
    <span className="value">
      {shown ? (
        <b>
          {fmt(props.value, props.digits ?? 3)} {props.unit || <em className="warn-text">(no unit!)</em>}
        </b>
      ) : (
        <span className="withheld">withheld{props.unit ? ` (${props.unit})` : ""}</span>
      )}{" "}
      <StatusBadge status={props.status} />{" "}
      <span className="badge muted">cal v{props.calVersion ?? "—"}</span> <QcBadge flag={props.qa} reason={props.qaReason} />
    </span>
  );
}

export function Loading({ error, loading }: { error?: string | null; loading?: boolean }) {
  if (error) return <div className="error">Error: {error}</div>;
  if (loading) return <div className="muted">Loading…</div>;
  return null;
}

export function Card({ title, children, right }: { title?: React.ReactNode; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="card">
      {(title || right) && (
        <div className="card-head">
          <h3>{title}</h3>
          <div>{right}</div>
        </div>
      )}
      {children}
    </section>
  );
}

export function LevelBadge({ level, name }: { level?: number | null; name?: string | null }) {
  if (!level) return <span className="badge ok">normal</span>;
  return <span className={`badge lvl${Math.min(level, 3)}`}>L{level}{name ? ` ${name}` : ""}</span>;
}

export function Counts({ obj }: { obj?: Record<string, number> | null }) {
  if (!obj) return null;
  return (
    <span>
      {Object.entries(obj).map(([k, v]) => (
        <span key={k} className={`badge ${k === "VERIFIED" || k === "OK" ? "ok" : k === "BAD" || k === "QC_BAD" ? "bad" : "warn"}`}>
          {k}: {v}
        </span>
      ))}
    </span>
  );
}
