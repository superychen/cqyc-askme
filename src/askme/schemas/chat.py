"""公开聊天协议。入口 api.routes.chat 校验后交给 ChatService，再由图节点执行。

所有下列例子均为演示数据；客户端仅提交问题/选项，不能指定 IP、证据或额度。
"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Question(BaseModel):
    """一次新问题；UUID 用于防重复计次。演示：message='你做过什么项目？'。"""
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def clean(cls, value: str) -> str:
        """拒绝只有空白的提问；演示 '  项目？ ' → '项目？'，全空白抛验证错误。"""
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class Selection(BaseModel):
    """选择服务端已生成的问题；不接收自由文本，防止免费续接被替换成任意问题。"""
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    option_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class QueryPlan(BaseModel):
    """LangChain 结构化查询计划；web_query 仅允许一般技术问题，不外发个人履历。"""
    standalone_query: str = Field(min_length=1, max_length=2000)
    intent: Literal["personal", "technical", "mixed", "other"]
    web_query: str = Field(default="", max_length=500)


class EvidenceDecision(BaseModel):
    """证据门控结果；allowed_ids 必须是实际候选，LLM 不能凭空生成来源。"""
    sufficient: bool
    allowed_ids: list[str] = Field(default_factory=list, max_length=10)
    missing: str = Field(default="", max_length=500)


class ClarificationPlan(BaseModel):
    """三条建议及其真实依据 ID；服务器校验失败则改用输入辅助选项。"""
    questions: list[str] = Field(min_length=3, max_length=3)
    evidence_ids: list[str] = Field(min_length=3, max_length=3)


SUGGESTIONS = [
    "请介绍一下你的工作年限和 Agent 开发经历。", "你最有代表性的 Agent 项目是什么？",
    "你在项目中如何设计 RAG 检索流程？", "你如何使用 LangChain 和 LangGraph？",
    "你如何设计 Agent 的工具调用？", "你如何处理多轮对话和上下文压缩？",
    "你如何减少模型幻觉、保证回答有依据？", "你如何评估 Agent 的效果？",
    "你如何处理 Agent 的超时、重试和异常？", "你如何优化 Agent 的响应速度和调用成本？",
]
