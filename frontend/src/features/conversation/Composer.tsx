import type { FormEvent } from "react";

import { LEGACY_TAKEOVER_NOTICE } from "../../presentation";

export function ConversationComposer({
  value,
  busy,
  mode,
  isNew,
  onChange,
  onSubmit,
}: {
  value: string;
  busy: boolean;
  mode: "agent" | "human_queue";
  isNew: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <form className="conversation-composer" onSubmit={onSubmit}>
      <textarea
        aria-label={isNew ? "开始新对话" : "继续提问"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={
          mode === "human_queue"
            ? "补充信息（消息仅记录，不会发送给人工坐席）…"
            : isNew
              ? "描述你遇到的问题…"
              : "继续提问或补充信息…"
        }
        rows={2}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
        }}
      />
      <div className="composer-bottom">
        <span>
          {mode === "human_queue"
            ? LEGACY_TAKEOVER_NOTICE
            : "Enter 发送 · Shift+Enter 换行"}
        </span>
        <button
          type="submit"
          disabled={busy || !value.trim()}
          aria-label="发送消息"
        >
          ➤
        </button>
      </div>
    </form>
  );
}
