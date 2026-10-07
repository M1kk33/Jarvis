"""Single-user loopback browser interface; policy stays in application services."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from importlib import resources

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.application.conversations import ConversationService
from jarvis.application.images import validate_png
from jarvis.application.jobs import JobRunner
from jarvis.application.speech import MAX_CLIP_BYTES, SpeechService
from jarvis.application.spoken import SpokenPlaybackService
from jarvis.application.tools import ToolService
from jarvis.application.vision import MAX_SCREENSHOT_BYTES, validate_screenshot
from jarvis.domain.jobs import TERMINAL_JOBS, Job, JobStatus
from jarvis.domain.resources import ResourceError
from jarvis.domain.speech import SpeechInvalid, SpeechUnavailable
from jarvis.domain.tools import InvocationStatus, ToolInvocation
from jarvis.domain.types import (
    ConversationError,
    ConversationNotFound,
    Message,
    RequestFailure,
    StaleTurnError,
)

BODY_LIMIT = 131_072
VISION_BODY_LIMIT = 3 * 1024 * 1024
COOKIE_NAME = "jarvis_session"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Setup(_Input):
    token: str = Field(min_length=1, max_length=128)


class _Conversation(_Input):
    title: str = Field(min_length=1, max_length=200)


class _Send(_Input):
    text: str = Field(min_length=1, max_length=65_536)
    submission_id: str = Field(min_length=1, max_length=200)


class _VisionSend(_Input):
    question: str = Field(min_length=1, max_length=1024)
    submission_id: str = Field(min_length=1, max_length=200)
    image_base64: str = Field(min_length=1, max_length=2_796_208)


class _Speak(_Input):
    conversation_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)


class _Empty(_Input):
    pass


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def _body[T: BaseModel](
    request: Request, model: type[T], limit: int = BODY_LIMIT
) -> T:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].lower()
        != "application/json"
    ):
        raise HTTPException(415, "JSON input required")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "Request body is too large")
    try:
        return model.model_validate(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        )
    except (UnicodeError, ValueError, ValidationError, RecursionError) as exc:
        raise HTTPException(422, "Invalid request body") from exc


def _message(message: Message) -> dict[str, object]:
    return {
        "id": message.id,
        "turn_id": message.turn_id,
        "role": message.role,
        "text": message.text,
        "position": message.position,
    }


def _job(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "turn_id": job.turn_id,
        "conversation_id": job.conversation_id,
        "status": job.status.value,
        "progress": job.progress,
        "attempt": job.attempt,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
    }


def create_web_app(
    service: ConversationService,
    runner: JobRunner,
    tool_service: ToolService | None,
    port: int,
    speech: SpeechService | None = None,
    spoken: SpokenPlaybackService | None = None,
) -> tuple[FastAPI, str]:
    """Create an isolated session; the bootstrap binds it to 127.0.0.1 only."""
    origin = f"http://127.0.0.1:{port}"
    setup_token = secrets.token_urlsafe(32)
    setup_digest = hashlib.sha256(setup_token.encode()).digest()
    session_digest: bytes | None = None
    csrf_token: str | None = None
    setup_consumed = False
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.headers.getlist("host") != [f"127.0.0.1:{port}"]:
            return Response(status_code=400)
        origins = request.headers.getlist("origin")
        if len(origins) > 1:
            return Response(status_code=403)
        supplied_origin = origins[0] if origins else None
        if supplied_origin is not None and supplied_origin != origin:
            return Response(status_code=403)
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site not in (None, "none", "same-origin"):
            return Response(status_code=403)
        lengths = request.headers.getlist("content-length")
        if len(lengths) > 1 or request.headers.get("transfer-encoding") is not None:
            return Response(status_code=400)
        length = lengths[0] if lengths else None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and length is None:
            return Response(status_code=411)
        if length is not None:
            try:
                if int(length) > (
                    MAX_CLIP_BYTES
                    if request.url.path == "/api/transcribe"
                    else VISION_BODY_LIMIT
                    if request.url.path.endswith("/vision")
                    else BODY_LIMIT
                ):
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' blob:; font-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def authorize(request: Request, *, mutate: bool = False) -> None:
        cookie = request.cookies.get(COOKIE_NAME, "")
        if (
            session_digest is None
            or not cookie
            or not hmac.compare_digest(
                hashlib.sha256(cookie.encode()).digest(), session_digest
            )
        ):
            raise HTTPException(401, "Local session required")
        if mutate and (
            csrf_token is None
            or not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf_token
            )
        ):
            raise HTTPException(403, "CSRF token required")

    @app.exception_handler(ConversationError)
    async def conversation_error(_request: Request, exc: ConversationError):
        code = 404 if isinstance(exc, (StaleTurnError, ConversationNotFound)) else 409
        return JSONResponse({"detail": str(exc)}, status_code=code)

    @app.exception_handler(ResourceError)
    async def resource_error(_request: Request, exc: ResourceError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(SpeechInvalid)
    async def speech_invalid(_request: Request, exc: SpeechInvalid):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(SpeechUnavailable)
    async def speech_unavailable(_request: Request, exc: SpeechUnavailable):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.post("/api/transcribe")
    async def transcribe(request: Request):
        authorize(request, mutate=True)
        if speech is None:
            raise HTTPException(404, "Speech is not configured")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        clip = bytearray()
        async for chunk in request.stream():
            clip.extend(chunk)
            if len(clip) > MAX_CLIP_BYTES:
                raise HTTPException(413, "Audio clip is too large")
        try:
            text = await speech.transcribe(bytes(clip), media_type)
        except asyncio.CancelledError:
            return JSONResponse({"detail": "Transcription cancelled"}, status_code=409)
        return {"text": text}

    @app.post("/api/transcribe/cancel")
    async def cancel_transcription(request: Request):
        authorize(request, mutate=True)
        if speech is None:
            raise HTTPException(404, "Speech is not configured")
        return {"cancelled": await speech.cancel()}

    @app.post("/api/spoken/start")
    async def start_spoken(request: Request):
        authorize(request, mutate=True)
        if spoken is None:
            raise HTTPException(404, "Spoken responses are not configured")
        supplied = await _body(request, _Speak)
        session_id, chunks = await spoken.start(
            supplied.conversation_id, supplied.turn_id, supplied.message_id
        )
        return {"session_id": session_id, "chunks": chunks}

    @app.post("/api/spoken/{session_id}/chunks/{index}")
    async def spoken_chunk(request: Request, session_id: str, index: int):
        authorize(request, mutate=True)
        if spoken is None:
            raise HTTPException(404, "Spoken responses are not configured")
        await _body(request, _Empty)
        audio = await spoken.chunk(session_id, index)
        if not await spoken.current(session_id):
            raise HTTPException(409, "Playback was cancelled")
        return Response(audio, media_type="audio/wav")

    @app.post("/api/spoken/{session_id}/stop")
    async def stop_spoken(request: Request, session_id: str):
        authorize(request, mutate=True)
        if spoken is None:
            raise HTTPException(404, "Spoken responses are not configured")
        await _body(request, _Empty)
        return {"stopped": await spoken.stop(session_id)}

    @app.get("/setup")
    async def setup_page(request: Request):
        if session_digest is not None:
            try:
                authorize(request)
                return RedirectResponse("/", status_code=303)
            except HTTPException:
                pass
        return Response(
            resources.files("jarvis")
            .joinpath("interfaces", "static", "setup.html")
            .read_bytes(),
            media_type="text/html",
        )

    @app.post("/api/setup")
    async def exchange_token(request: Request):
        nonlocal setup_consumed, session_digest, csrf_token
        supplied = await _body(request, _Setup)
        if setup_consumed or not hmac.compare_digest(
            hashlib.sha256(supplied.token.encode()).digest(), setup_digest
        ):
            raise HTTPException(403, "Setup token is invalid or already used")
        setup_consumed = True
        session = secrets.token_urlsafe(32)
        session_digest = hashlib.sha256(session.encode()).digest()
        csrf_token = secrets.token_urlsafe(32)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            session,
            httponly=True,
            secure=False,  # Loopback HTTP; binding and Host/Origin checks are mandatory.
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/")
    async def home(request: Request):
        try:
            authorize(request)
        except HTTPException:
            return RedirectResponse("/setup", status_code=303)
        return Response(
            resources.files("jarvis")
            .joinpath("interfaces", "static", "index.html")
            .read_bytes(),
            media_type="text/html",
        )

    @app.get("/static/{filename}")
    async def static_file(filename: str):
        if filename not in {"app.css", "app.js", "voice_player.js"}:
            raise HTTPException(404)
        media = "text/css" if filename.endswith(".css") else "text/javascript"
        return Response(
            resources.files("jarvis")
            .joinpath("interfaces", "static", filename)
            .read_bytes(),
            media_type=media,
        )

    @app.get("/api/session")
    async def session(request: Request):
        authorize(request)
        return {
            "csrf_token": csrf_token,
            "speech_enabled": speech is not None,
            "spoken_enabled": spoken is not None,
            "vision_enabled": runner.vision is not None,
        }

    @app.get("/api/conversations")
    async def conversations(request: Request):
        authorize(request)
        items = await service.store.run(service.store.list_conversations)
        return {"conversations": [asdict(item) for item in items]}

    @app.post("/api/conversations")
    async def create_conversation(request: Request):
        authorize(request, mutate=True)
        supplied = await _body(request, _Conversation)
        conversation = await service.store.run(
            lambda: service.store.create_conversation(supplied.title)
        )
        return JSONResponse(asdict(conversation), status_code=201)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(request: Request, conversation_id: str):
        authorize(request)

        def snapshot():
            record, turns, messages = service.resume(conversation_id)
            drafts = [
                draft
                for turn in turns
                if (draft := service.store.get_draft(turn.id)) is not None
            ]
            artifacts = service.store.list_artifacts_for_conversation(conversation_id)
            return record, turns, messages, drafts, artifacts

        record, turns, messages, drafts, artifacts = await service.store.run(snapshot)
        return {
            "conversation": asdict(record),
            "turns": [asdict(turn) for turn in turns],
            "messages": [_message(message) for message in messages],
            "drafts": [asdict(draft) for draft in drafts],
            "artifacts": artifacts,
        }

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send(request: Request, conversation_id: str):
        authorize(request, mutate=True)
        supplied = await _body(request, _Send)
        job, duplicate = await runner.submit(
            conversation_id, supplied.text, supplied.submission_id
        )
        if not duplicate and job.status is JobStatus.QUEUED:
            runner.launch(job.id)
        return JSONResponse(
            {"job": _job(job), "duplicate": duplicate},
            status_code=200 if duplicate else 202,
        )

    @app.post("/api/conversations/{conversation_id}/vision")
    async def send_vision(request: Request, conversation_id: str):
        authorize(request, mutate=True)
        if runner.vision is None:
            raise HTTPException(404, "Vision is not configured")
        supplied = await _body(request, _VisionSend, VISION_BODY_LIMIT)
        try:
            image = base64.b64decode(supplied.image_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, "Screenshot is not valid base64") from exc
        if len(image) > MAX_SCREENSHOT_BYTES:
            raise HTTPException(413, "Screenshot is too large")
        try:
            validate_screenshot(image)
        except RequestFailure as exc:
            raise HTTPException(422, str(exc)) from exc
        job, duplicate = await runner.submit_vision(
            conversation_id, supplied.question, image, supplied.submission_id
        )
        if not duplicate and job.status is JobStatus.QUEUED:
            runner.launch(job.id)
        return JSONResponse(
            {"job": _job(job), "duplicate": duplicate},
            status_code=200 if duplicate else 202,
        )

    @app.get("/api/jobs")
    async def jobs(request: Request):
        authorize(request)
        return {"jobs": [_job(item) for item in await runner.list_jobs()]}

    async def job_snapshot(job_id: str) -> dict[str, object]:
        job = await runner.status(job_id)
        draft = await service.store.run(lambda: service.store.get_draft(job.turn_id))
        return {"job": _job(job), "draft": draft.text if draft is not None else None}

    @app.get("/api/jobs/{job_id}")
    async def job_status(request: Request, job_id: str):
        authorize(request)
        return await job_snapshot(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(request: Request, job_id: str):
        authorize(request, mutate=True)
        return {"job": _job(await runner.cancel(job_id))}

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(request: Request, job_id: str):
        authorize(request, mutate=True)
        job = await runner.status(job_id)
        if job.status not in {
            JobStatus.QUEUED,
            JobStatus.INTERRUPTED,
            JobStatus.WAITING_APPROVAL,
        }:
            raise HTTPException(409, "Job cannot resume")
        if job.profile_name not in {
            runner.profile_identity,
            runner.vision.profile_identity if runner.vision is not None else "",
        }:
            raise HTTPException(409, "Job requires its original pinned profile")
        runner.launch(job_id)
        return JSONResponse({"job": _job(job)}, status_code=202)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str):
        authorize(request)
        await runner.status(job_id)

        async def stream() -> AsyncIterator[str]:
            previous: str | None = None
            while not await request.is_disconnected():
                snapshot = await job_snapshot(job_id)
                encoded = json.dumps(
                    snapshot, ensure_ascii=False, separators=(",", ":")
                )
                if encoded != previous:
                    yield f"event: progress\ndata: {encoded}\n\n"
                    previous = encoded
                job = snapshot["job"]
                if isinstance(job, dict) and job.get("status") in {
                    item.value for item in TERMINAL_JOBS
                }:
                    break
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def waiting_invocation(invocation_id: str) -> ToolInvocation:
        if tool_service is None:
            raise HTTPException(404)
        invocation = await tool_service.store.run(
            lambda: tool_service.store.get_invocation(invocation_id)
        )
        if (
            invocation.job_id is None
            or invocation.status is not InvocationStatus.WAITING_APPROVAL
        ):
            raise HTTPException(409, "Approval is no longer pending")
        job = await runner.status(invocation.job_id)
        if (
            job.status is not JobStatus.WAITING_APPROVAL
            or job.attempt != invocation.job_attempt
        ):
            raise HTTPException(409, "Approval belongs to an inactive attempt")
        return invocation

    @app.get("/api/approvals")
    async def approvals(request: Request):
        authorize(request)
        items: list[dict[str, object]] = []
        if tool_service is not None:
            for job in await runner.list_jobs():
                if job.status is not JobStatus.WAITING_APPROVAL:
                    continue
                invocations = await service.store.run(
                    partial(service.store.get_invocations, job.turn_id)
                )
                for invocation in invocations:
                    if (
                        invocation.status is InvocationStatus.WAITING_APPROVAL
                        and invocation.job_attempt == job.attempt
                    ):
                        approval = await tool_service.store.run(
                            partial(tool_service.store.get_tool_approval, invocation.id)
                        )
                        actionable = approval is None or (
                            approval.status == "issued"
                            and datetime.fromisoformat(approval.expires_at)
                            > datetime.now(UTC)
                        )
                        items.append(
                            {
                                "invocation_id": invocation.id,
                                "job_id": job.id,
                                "turn_id": invocation.turn_id,
                                "name": invocation.name,
                                "version": invocation.version,
                                "arguments": invocation.arguments_json,
                                "target": invocation.target_identity,
                                "policy_version": invocation.policy_version,
                                "actionable": actionable,
                                "preview": (
                                    json.loads(invocation.arguments_json)
                                    if invocation.name in {"create_text_file", "fetch_public_page"}
                                    else None
                                ),
                            }
                        )
        uncertain: list[dict[str, str]] = []
        if tool_service is not None and tool_service.file_service is not None:
            records = await service.store.run(
                service.store.list_unknown_file_invocations
            )
            uncertain = [
                {
                    "invocation_id": item.id,
                    "job_id": item.job_id or "",
                    "path": json.loads(item.arguments_json)["path"],
                }
                for item in records
            ]
        return {"approvals": items, "uncertain_files": uncertain}

    @app.post("/api/file-reconciliation/{invocation_id}")
    async def reconcile_file(request: Request, invocation_id: str):
        authorize(request, mutate=True)
        if tool_service is None or tool_service.file_service is None:
            raise HTTPException(404)
        result = await tool_service.reconcile_file(invocation_id)
        if result is None:
            raise HTTPException(409, "Exact file and hash were not verified")
        return {"invocation_id": result.id, "status": result.status.value}

    @app.post("/api/approvals/{invocation_id}/approve")
    async def approve(request: Request, invocation_id: str):
        authorize(request, mutate=True)
        invocation = await waiting_invocation(invocation_id)
        assert tool_service is not None and invocation.job_id is not None
        approval_id = await tool_service.trusted_issue_approval(invocation_id)
        await tool_service.execute(invocation_id, approval_id)
        runner.launch(invocation.job_id)
        return JSONResponse({"job_id": invocation.job_id}, status_code=202)

    @app.post("/api/approvals/{invocation_id}/reject")
    async def reject(request: Request, invocation_id: str):
        authorize(request, mutate=True)
        invocation = await waiting_invocation(invocation_id)
        assert invocation.job_id is not None
        job = await runner.cancel(invocation.job_id)
        return {"job": _job(job)}

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(request: Request, artifact_id: str):
        authorize(request)
        try:
            data = await runner.store.run(
                lambda: runner.store.get_artifact_bytes(artifact_id)
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Artifact is unavailable") from exc
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="artifact.bin"'},
        )

    @app.get("/api/artifacts/{artifact_id}/preview")
    async def image_preview(request: Request, artifact_id: str):
        authorize(request)
        try:
            data = await runner.store.run(
                lambda: runner.store.get_artifact_bytes(artifact_id)
            )
            try:
                validate_png(data)
            except RequestFailure:
                validate_screenshot(data)
        except (ValueError, OSError, RequestFailure) as exc:
            raise HTTPException(404, "Image is unavailable") from exc
        return Response(
            data, media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    return app, setup_token
