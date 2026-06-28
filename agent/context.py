"""上下文管理：token 估算与历史压缩。"""

from agent.config import ContextConfig
from agent.llm import LLMClient
from agent.llm.schema import Message

SUMMARY_PROMPT = """请用中文总结以下对话的关键信息，控制在 300 字以内。
必须保留：
1. 用户的原始目标或任务
2. 已经确认的方案或决策
3. 未完成的待办事项
4. 重要的文件路径或代码改动

对话记录：
{conversation}

摘要："""


def _format_message_for_summary(msg: Message) -> str:
    if msg.role == "assistant" and msg.tool_calls:
        calls = ", ".join(f"{tc.name}({tc.arguments})" for tc in msg.tool_calls)
        return f"[{msg.role}] 调用工具: {calls}"
    if msg.role == "tool":
        return f"[{msg.role}] 工具结果 (id={msg.tool_call_id}): {msg.content}"
    return f"[{msg.role}] {msg.content or ''}"


class ContextManager:
    """管理 REPL 的消息列表，提供 token 估算和上下文压缩。"""

    def __init__(
        self,
        messages: list[Message],
        config: ContextConfig | None = None,
    ):
        self.messages = messages
        self.config = config or ContextConfig()

    def estimate_tokens(self) -> int:
        """粗略估算当前消息列表的 token 数。

        采用字符类型加权：CJK 字符约 1 token/字，ASCII 约 0.25 token/字
        （4 字符 ≈ 1 token）。之前的 ``len // 4`` 对中文严重低估（把一个
        中文字算成 0.25 token，实际约 1-2 token），导致 is_near_limit 误
        判"还有空间"而实际已超限，引发 LLM 400 错误。
        """
        total = 0
        for msg in self.messages:
            # system/user/assistant/tool 基础开销
            total += 50
            content = msg.content or ""
            cjk = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
            other = len(content) - cjk
            total += cjk + max(other // 4, 1)
            if msg.tool_calls:
                total += len(msg.tool_calls) * 100
        return total

    def is_near_limit(self) -> bool:
        """当前上下文是否接近配置阈值。"""
        return self.estimate_tokens() >= self.config.max_tokens

    def compact(self, llm_client: LLMClient) -> bool:
        """手动压缩历史消息。保留 system + 最近 N 条，其余生成摘要。

        返回是否发生了压缩。
        """
        preserve = max(self.config.preserve_recent, 2)
        if len(self.messages) <= 1 + preserve:
            return False

        system_msg = self.messages[0]
        recent = self.messages[-preserve:]
        to_compress = self.messages[1:-preserve]

        conversation = "\n\n".join(_format_message_for_summary(m) for m in to_compress)
        prompt = SUMMARY_PROMPT.format(conversation=conversation)

        response = llm_client.chat([Message(role="user", content=prompt)])
        summary = response.content or "（摘要生成失败，已保留最近对话）"

        self.messages[:] = [
            system_msg,
            Message(role="system", content=f"[上下文摘要] {summary}"),
            *recent,
        ]
        return True
