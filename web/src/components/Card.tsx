import type { ReactNode } from "react";
import { Panel } from "./ui";

/** 旧来の名前（各画面で使用）。見た目は Panel と同じ。 */
export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return <Panel title={title}>{children}</Panel>;
}
