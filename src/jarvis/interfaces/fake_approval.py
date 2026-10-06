"""Trusted test seam for M5 approvals; model text never invokes this interface."""

from jarvis.application.tools import ToolService


class FakeApprovalUi:
    def __init__(self, service: ToolService) -> None:
        self.service = service

    async def approve(self, invocation_id: str) -> str:
        return await self.service.trusted_issue_approval(invocation_id)
