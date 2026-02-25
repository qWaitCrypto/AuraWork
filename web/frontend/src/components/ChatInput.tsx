import React, { useRef, useCallback } from "react";
import { ArrowUp, Paperclip } from "lucide-react";

export interface ChatInputProps {
    draft: string;
    onDraftChange: (value: string) => void;
    onSend: () => void;
    disabled?: boolean;
    placeholder?: string;
}

export const ChatInput = React.memo(function ChatInput({
    draft,
    onDraftChange,
    onSend,
    disabled,
    placeholder = "Message Aura...",
}: ChatInputProps) {
    const textareaRef = useRef<HTMLTextAreaElement | null>(null);

    const autosize = useCallback(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.style.height = "auto";
        el.style.height = `${el.scrollHeight}px`;
    }, []);

    const handleChange = useCallback(
        (e: React.ChangeEvent<HTMLTextAreaElement>) => {
            onDraftChange(e.target.value);
            queueMicrotask(autosize);
        },
        [onDraftChange, autosize],
    );

    const handleKeyDown = useCallback(
        (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSend();
            }
        },
        [onSend],
    );

    return (
        <div className="p-4 pb-6 w-full">
            <div className="relative rounded-2xl border border-surface-200 bg-surface-0 shadow-elevated transition-all focus-within:border-accent-300 focus-within:ring-2 focus-within:ring-accent-100">
                <textarea
                    ref={textareaRef}
                    rows={1}
                    placeholder={placeholder}
                    className="max-h-48 w-full resize-none bg-transparent px-4 py-4 pr-24 text-sm text-ink-900 outline-none placeholder:text-ink-400 scrollbar-hide"
                    value={draft}
                    onChange={handleChange}
                    onKeyDown={handleKeyDown}
                    onInput={autosize}
                />
                <div className="absolute bottom-2 right-2 flex gap-1">
                    <button
                        className="rounded-lg p-2 text-ink-400 opacity-40 cursor-not-allowed"
                        title="Attach file — Coming soon"
                        type="button"
                        disabled
                    >
                        <Paperclip className="h-4 w-4" />
                    </button>
                    <button
                        className="rounded-xl bg-accent-600 p-2.5 text-white shadow-medium transition-all hover:scale-105 hover:bg-accent-700 active:scale-95 disabled:opacity-60"
                        title="Send"
                        type="button"
                        onClick={onSend}
                        disabled={disabled || !draft.trim()}
                    >
                        <ArrowUp className="h-4 w-4" />
                    </button>
                </div>
            </div>
            <div className="mt-2 text-center text-[10px] text-ink-400">
                Aura may produce inaccurate information. Please verify important details.
            </div>
        </div>
    );
});
