/** Tiny markdown renderer — covers headings / bold / italic / code / lists /
 *  tables / blockquotes. Avoids pulling in a markdown lib for the v0.1 chat
 *  page; the agent's output is constrained by the system prompt. */

import type { ReactNode } from "react";

export function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Table block: |...| header line followed by |---| separator
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s:|-]+\|/.test(lines[i + 1])) {
      const headerCells = parseRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].startsWith("|")) {
        rows.push(parseRow(lines[i]));
        i += 1;
      }
      blocks.push(
        <table key={key++}>
          <thead>
            <tr>{headerCells.map((c, ci) => <th key={ci}>{renderInline(c)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>{r.map((c, ci) => <td key={ci}>{renderInline(c)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }

    // Heading
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = Math.min(h[1].length + 1, 6);
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key++}>{renderInline(h[2])}</Tag>);
      i += 1;
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      blocks.push(
        <ul key={key++}>{items.map((it, ii) => <li key={ii}>{renderInline(it)}</li>)}</ul>,
      );
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      blocks.push(
        <ol key={key++}>{items.map((it, ii) => <li key={ii}>{renderInline(it)}</li>)}</ol>,
      );
      continue;
    }

    // Blockquote
    if (line.startsWith("> ")) {
      blocks.push(<blockquote key={key++}>{renderInline(line.slice(2))}</blockquote>);
      i += 1;
      continue;
    }

    // Blank line
    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Plain paragraph
    blocks.push(<p key={key++}>{renderInline(line)}</p>);
    i += 1;
  }

  return <>{blocks}</>;
}

function parseRow(line: string): string[] {
  const trimmed = line.trim();
  const inner = trimmed.startsWith("|") ? trimmed.slice(1) : trimmed;
  const stripped = inner.endsWith("|") ? inner.slice(0, -1) : inner;
  return stripped.split("|").map((c) => c.trim());
}

function renderInline(text: string): ReactNode {
  // Order matters: code first (so we don't apply bold inside code), then bold, then italic
  const parts: ReactNode[] = [];
  let rest = text;
  let key = 0;
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*/;
  while (true) {
    const m = re.exec(rest);
    if (!m) {
      parts.push(rest);
      break;
    }
    if (m.index > 0) parts.push(rest.slice(0, m.index));
    if (m[1] !== undefined) parts.push(<code key={key++}>{m[1]}</code>);
    else if (m[2] !== undefined) parts.push(<strong key={key++}>{m[2]}</strong>);
    else if (m[3] !== undefined) parts.push(<em key={key++}>{m[3]}</em>);
    rest = rest.slice(m.index + m[0].length);
  }
  return parts;
}
