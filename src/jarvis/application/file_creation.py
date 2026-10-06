"""Durable create-only file action and exact post-crash reconciliation."""

from __future__ import annotations

import asyncio
import json

from jarvis.application.resource_manager import _settle
from jarvis.domain.ports import FileCreator, ToolStore
from jarvis.domain.tools import InvocationStatus, ToolInvocation
from jarvis.domain.types import NoEffectFailure, RequestFailure


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FileCreateService:
    def __init__(self, store: ToolStore, creator: FileCreator) -> None:
        self.store = store
        self.creator = creator

    def inspect(self, arguments: dict[str, object]) -> str:
        return self.creator.inspect(str(arguments["path"]), str(arguments["content"]))

    @staticmethod
    def _result(data: dict[str, object]) -> tuple[str, str]:
        receipt = _canonical({"status": "complete", **data})
        result = _canonical({"status": "ok", "data": data})
        return receipt, result

    async def run(
        self, invocation: ToolInvocation, arguments: dict[str, object]
    ) -> ToolInvocation:
        worker = asyncio.create_task(
            asyncio.to_thread(
                self.creator.create,
                str(arguments["path"]),
                str(arguments["content"]),
                invocation.target_identity,
            )
        )
        cancelled = False
        try:
            try:
                data = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
                data = await _settle(worker)
        except NoEffectFailure as exc:
            receipt = _canonical({"status": "not_executed", "code": exc.code})
            result = _canonical({"status": "error", "code": exc.code})
        else:
            receipt, result = self._result(data)
        commit_task = asyncio.create_task(
            self.store.run(
                lambda: self.store.commit_tool_result(invocation.id, receipt, result)
            )
        )
        try:
            committed = await _settle(commit_task)
        except Exception:
            current = await _settle(
                asyncio.create_task(
                    self.store.run(lambda: self.store.get_invocation(invocation.id))
                )
            )
            if current.status is not InvocationStatus.COMPLETE:
                raise
            committed = current
        if cancelled:
            raise asyncio.CancelledError
        return committed

    async def reconcile(self, invocation: ToolInvocation) -> ToolInvocation | None:
        if (
            invocation.name != "create_text_file"
            or invocation.status is not InvocationStatus.OUTCOME_UNKNOWN
        ):
            raise RequestFailure(
                "invalid_request", "No uncertain file creation to verify"
            )
        arguments = json.loads(invocation.arguments_json)
        data = await asyncio.to_thread(
            self.creator.reconcile,
            arguments["path"],
            arguments["content"],
            invocation.target_identity,
        )
        if data is None:
            return None
        receipt, result = self._result(data)
        try:
            return await _settle(
                asyncio.create_task(
                    self.store.run(
                        lambda: self.store.reconcile_tool_result(
                            invocation.id, receipt, result
                        )
                    )
                )
            )
        except Exception:
            await self.store.run(lambda: self.store.get_invocation(invocation.id))
            raise
