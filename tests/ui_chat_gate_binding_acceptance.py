"""Static acceptance for gate-bound answers in the dashboard chat UI.

The browser form must bind replies to the gate id carried by the NEEDS-YOU chat
message. A generic chat send is deliberately unbound.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "ui" / "static" / "app.js"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    source = APP_JS.read_text()
    form_start = source.index('elements.chatForm.addEventListener("submit"')
    generic_chat_form = source[form_start:]

    _check("chat messages read explicit gate ids", "function gateQuestionIdForMessage" in source, "missing helper")
    _check("chat render receives open question set", "renderChatMessages(chat.messages || [], chat.open_questions || [])" in source, "missing open question binding")
    _check("chat NEEDS-YOU message renders answer control", "data-chat-answer-question" in source and "chat-gate-answer" in source, "missing gate answer control")
    _check("chat answer posts exact reply_to_question_id", "reply_to_question_id: questionId" in source, "missing exact gate binding")
    _check("generic chat send remains unbound", "reply_to_question_id" not in generic_chat_form, "generic form must not infer a gate")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - dashboard chat answers bind to explicit gate ids only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
