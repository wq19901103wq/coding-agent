"""Data models for multi-agent goal management and IPC."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Goal(BaseModel):
    id: str
    parent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    title: str
    description: str = ""
    agent_role: str
    status: GoalStatus = GoalStatus.PENDING
    priority: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    error_log: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)

    def model_dump(self, **kwargs) -> dict[str, Any]:
        data = super().model_dump(**kwargs)
        # Ensure enum and datetime are serialized consistently for storage.
        data["status"] = self.status.value
        data["created_at"] = self.created_at.isoformat()
        if self.started_at:
            data["started_at"] = self.started_at.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()
        return data


class AgentRole(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    max_steps_per_turn: int | None = None
    temperature: float | None = None


class MessageType(str, Enum):
    READY = "ready"
    ASSIGN_GOAL = "assign_goal"
    STATUS_UPDATE = "status_update"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    NEED_CONFIRM = "need_confirm"
    USER_INPUT = "user_input"
    COMPLETE = "complete"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


class IPCMessage(BaseModel):
    msg_id: str
    goal_id: str | None = None
    type: MessageType
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def model_dump(self, **kwargs) -> dict[str, Any]:
        data = super().model_dump(**kwargs)
        data["type"] = self.type.value
        data["timestamp"] = self.timestamp.isoformat()
        return data
