from __future__ import annotations
from typing import Any
import logging
from sqlalchemy import text
from app.database.connection import engine
from app.database.notification_repository import get_next_manual_notification_for_user
from app.database.user_repository import get_user
from app.llm.generate_engagement_sentiment_notification import (
    build_engagement_sentiment_notification_prompt,
)
from app.llm.generate_performance_notification import (
    build_performance_notification_prompt,
)
from app.llm.generate_qa_sentiment_notification import (
    build_no_qa_sentiment_notification_prompt,
    build_qa_sentiment_notification_prompt,
)
from app.llm.llm_client import NotificationGenerator
from app.notifications.engine import NotificationEngine
from app.notifications.models import NotificationProcessingResult, NotificationRequest
from app.notifications.sender import NotificationSender
from app.constants import FLOW_BY_EVENT_TYPE
from app.performance.performance import calculate_performance
from app.recommendation.recommendation import embed_text, recommend_video
from app.sentiment.sentiment import (
    get_eligible_user_qa,
    get_user_response_rate,
    prepare_user_qa,
    save_sentiment_notification_history,
)

logger = logging.getLogger(__name__)

NO_ENGAGEMENT_DATA = "NO_ENGAGEMENT_DATA"
NO_VIDEO_RECOMMENDATION = "NO_VIDEO_RECOMMENDATION"
LLM_NO_RESPONSE = "LLM_NO_RESPONSE"


def _is_terminal_output_error(error: Exception) -> bool:
    message = str(error).lower()
    error_type = type(error).__name__.lower()
    return (
        any(marker in message for marker in (
            "insufficient_quota", "quota exceeded", "billing", "credit exhausted",
            "out of credits", "exceeded your current quota", "usage limit",
        ))
        or any(marker in error_type for marker in (
            "connecterror", "connecttimeout", "readtimeout", "timeout", "connectionerror",
        ))
        or any(marker in message for marker in (
            "connection refused", "connection error", "connecterror", "connecttimeout",
            "readtimeout", "network is unreachable", "name or service not known",
            "temporary failure in name resolution", "failed to establish a new connection",
            "all connection attempts failed", "remote end closed connection",
        ))
    )
MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"


def _selected_question_without_answers(prepared_qa: dict[str, Any]) -> str | None:
    questions = prepared_qa.get("questions") or []
    if not questions:
        return None
    selected = questions[0]
    responses = selected.get("responses") or []
    if any(response.get("answer") is not None for response in responses):
        return None
    return next(
        (response.get("question") for response in responses if response.get("question")),
        None,
    )


class NotificationService:
    """Coordinate the AI-CLAN notification pipeline."""

    def __init__(
        self,
        sender: NotificationSender | None = None,
        generator: NotificationGenerator | None = None,
        db_engine=engine,
    ) -> None:
        self.sender = sender or NotificationSender()
        self.generator = generator or NotificationGenerator()
        self.db_engine = db_engine
        self.engine = NotificationEngine()
        self.last_skip_reason: str | None = None
        self.last_error: str | None = None
        self.last_error_type: str | None = None

    def _get_user_profile(
        self,
        user_id: int,
    ) -> dict[str, Any]:
        profile = get_user(user_id, self.db_engine)
        if profile is None:
            self.last_skip_reason = MISSING_REQUIRED_DATA
            return None

        language_code = str(profile.get("app_language_code") or "").strip().lower()
        if not language_code:
            self.last_skip_reason = MISSING_REQUIRED_DATA
            return None
        video_language_ids = profile.get("video_language_ids") or []

        return {
            "user_name": profile.get("user_name") or profile.get("name") or f"User {user_id}",
            "app_language_code": language_code,
            "video_language_ids": [int(value) for value in video_language_ids],
        }

    def _fallback_notification(self, flow: str, user_name: str, context: str) -> dict[str, str]:
        if flow == "engagement":
            return {
                "title": f"{user_name}, Keep Building Momentum",
                "description": f"Your CLAN engagement matters. Keep participating so your progress stays strong and consistent.{context}",
            }
        if flow == "sentiment":
            return {
                "title": f"{user_name}, Your Progress Matters",
                "description": f"Your recent responses show a clear opportunity to keep growing with practical next steps.{context}",
            }
        return {
            "title": f"{user_name}, Focus on {context}",
            "description": "Keep refining the area that is currently your biggest opportunity and build momentum with small, consistent actions.",
        }

    def _get_telugu_video_language_id(self) -> int | None:
        query = text(
            """
            SELECT id
            FROM public.md_language
            WHERE lower(code) = 'te'
               OR lower(name) = 'telugu'
            ORDER BY CASE WHEN lower(code) = 'te' THEN 0 ELSE 1 END, id
            LIMIT 1
            """
        )
        with self.db_engine.connect() as connection:
            language_id = connection.execute(query).scalar_one_or_none()
        return int(language_id) if language_id is not None else None

    def _make_result(
        self,
        *,
        user_id: int,
        notification_type: str,
        title: str,
        description: str,
        reference_id: int,
        video_popup: bool | None,
        flow: str,
        should_send: bool,
        reason: str | None = None,
        history_records: list[dict] | None = None,
    ) -> NotificationProcessingResult:
        notification = self.engine.make_notification(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            description=description,
            reference_id=reference_id,
            video_popup=video_popup,
        )

        response = NotificationProcessingResult(
            user_id=notification.user_id,
            title=notification.title,
            description=notification.description,
            notification_type=notification.notification_type,
            reference_id=notification.reference_id,
            video_popup=notification.video_popup,
            image=notification.image,
            flow=flow,
            should_send=should_send,
            reason=reason,
        )

        if should_send:
            if not self.sender.remote_url:
                response.remote_send_status = "skipped"
                response.reason = reason or "REMOTE_NOTIFICATION_SEND_URL is not configured"
                response.error = "REMOTE_NOTIFICATION_SEND_URL is not configured"
            else:
                try:
                    remote_response = self.sender.send(
                        user_id=notification.user_id,
                        notification_type=notification.notification_type,
                        title=notification.title,
                        description=notification.description,
                        reference_id=notification.reference_id,
                        video_popup=notification.video_popup,
                        image=notification.image,
                    )
                    response.remote_send_status = "sent"
                    response.remote_send_response = remote_response
                    if flow == "sentiment" and history_records:
                        save_sentiment_notification_history(
                            history_records,
                            self.db_engine,
                        )
                except Exception as exc:  # pragma: no cover - defensive fallback
                    if not _is_terminal_output_error(exc):
                        logger.exception(
                            "Notification send failed for user=%s flow=%s",
                            user_id,
                            flow,
                        )
                    response.remote_send_status = "failed"
                    response.error = str(exc)
                    response.remote_send_error_type = type(exc).__name__
                    response.reason = reason or response.error

        return response

    def _generate_llm_notification(self, flow: str, user_name: str, payload: dict[str, Any]) -> dict[str, str]:
        if flow == "engagement":
            prompt = build_engagement_sentiment_notification_prompt(
                user_name=user_name,
                language=payload.get("language", "English"),
                response_data=payload["response_data"],
            )
        elif flow == "sentiment":
            if payload.get("prepared_qa"):
                question = _selected_question_without_answers(payload["prepared_qa"])
                if question is not None:
                    prompt = build_no_qa_sentiment_notification_prompt(
                        user_name=user_name,
                        language=payload.get("language", "English"),
                        question=question,
                    )
                else:
                    prompt = build_qa_sentiment_notification_prompt(
                        user_name=user_name,
                        language=payload.get("language", "English"),
                        prepared_qa=payload["prepared_qa"],
                    )
            else:
                prompt = build_no_qa_sentiment_notification_prompt(
                    user_name=user_name,
                    language=payload.get("language", "English"),
                )
        else:
            prompt = build_performance_notification_prompt(
                user_name=user_name,
                language=payload["language"],
                weakest_kii=payload["weakest_kii"],
                video=payload["video"],
            )
        notification_type = {
            "performance": "VIDEO_RECOMMENDATION",
            "engagement": "SENTIMENT_ENGAGEMENT",
            "sentiment": "SENTIMENT_QA",
        }[flow]
        return self.generator.generate(
            prompt,
            user_id=payload.get("user_id"),
            notification_type=notification_type,
        )

    async def _generate_llm_notification_async(self, flow: str, user_name: str, payload: dict[str, Any]) -> dict[str, str]:
        if flow == "engagement":
            prompt = build_engagement_sentiment_notification_prompt(
                user_name=user_name,
                language=payload.get("language", "English"),
                response_data=payload["response_data"],
            )
        elif flow == "sentiment":
            if payload.get("prepared_qa"):
                question = _selected_question_without_answers(payload["prepared_qa"])
                if question is not None:
                    prompt = build_no_qa_sentiment_notification_prompt(
                        user_name=user_name,
                        language=payload.get("language", "English"),
                        question=question,
                    )
                else:
                    prompt = build_qa_sentiment_notification_prompt(
                        user_name=user_name,
                        language=payload.get("language", "English"),
                        prepared_qa=payload["prepared_qa"],
                    )
            else:
                prompt = build_no_qa_sentiment_notification_prompt(
                    user_name=user_name,
                    language=payload.get("language", "English"),
                )
        else:
            prompt = build_performance_notification_prompt(
                user_name=user_name,
                language=payload["language"],
                weakest_kii=payload["weakest_kii"],
                video=payload["video"],
            )
        notification_type = {
            "performance": "VIDEO_RECOMMENDATION",
            "engagement": "SENTIMENT_ENGAGEMENT",
            "sentiment": "SENTIMENT_QA",
        }[flow]
        return await self.generator.generate_async(
            prompt,
            user_id=payload.get("user_id"),
            notification_type=notification_type,
        )

    def _build_flow_context(
        self,
        user_id: int,
        flow: str,
    ) -> tuple[str, dict[str, Any]] | None:
        profile = self._get_user_profile(user_id)
        if profile is None:
            return None
        user_name = str(profile["user_name"]).strip() or f"User {user_id}"
        language = str(profile["app_language_code"]).strip()
        if flow == "engagement":
            response_data = get_user_response_rate(user_id)
            if not response_data or response_data["questions_sent"] == 0:
                self.last_skip_reason = NO_ENGAGEMENT_DATA
                return None
            return user_name, {
                "user_id": user_id,
                "language": language,
                "response_data": response_data,
                "context": "your CLAN participation",
            }
        if flow == "sentiment":
            eligible_responses = get_eligible_user_qa(user_id, self.db_engine)
            if not eligible_responses:
                self.last_skip_reason = MISSING_REQUIRED_DATA
                return None
            selected = eligible_responses[0]
            logger.info(
                "[SENTIMENT QA SELECTION] user_id=%s selected_question_id=%s selected_question=%r used_question_ids=%s unused_question_ids=%s answer_ids=%s answer_texts=%s",
                user_id,
                selected.get("question_id"),
                selected.get("question"),
                selected.get("used_question_ids", []),
                selected.get("unused_question_ids", []),
                [row.get("answer_id") for row in eligible_responses if row.get("answer_id") is not None],
                [row.get("answer") for row in eligible_responses if row.get("answer") is not None],
            )
            prepared_qa = prepare_user_qa(
                user_id,
                self.db_engine,
                rows=eligible_responses,
            )
            return user_name, {
                "user_id": user_id,
                "language": language,
                "prepared_qa": prepared_qa,
                "history_records": eligible_responses,
                "context": "your recent responses and workplace reflections",
            }

        # Performance calculation and video retrieval remain separate concerns.
        language_id = profile["video_language_ids"]
        if not language_id:
            telugu_language_id = self._get_telugu_video_language_id()
            if telugu_language_id is None:
                self.last_skip_reason = MISSING_REQUIRED_DATA
                return None
            language_id = [telugu_language_id]
        calc = calculate_performance(user_id)
        weakest = calc.get("improvement_area")
        if not weakest:
            self.last_skip_reason = MISSING_REQUIRED_DATA
            return None
        performance = type(
            "PerformanceContext",
            (),
            {"kii_name": weakest["kii_name"], "kii_id": weakest["kii_id"]},
        )()
        recommendation = recommend_video(
            performance,
            language_id,
            embed_text,
            self.db_engine,
            user_id=user_id,
        )
        if recommendation is None:
            self.last_skip_reason = NO_VIDEO_RECOMMENDATION
            return None

        return user_name, {
            "user_id": user_id,
            "language": language,
            "weakest_kii": weakest,
            "video": recommendation,
            "video_title": recommendation.get("title"),
            "video_id": recommendation.get("video_id"),
            "context": weakest.get("kii_name", "your current growth area"),
        }

    def get_performance(self, user_id: int) -> dict[str, Any]:
        result = calculate_performance(user_id)
        return {
            "user_id": user_id,
            "seven_day_performance": result["performance"],
            "weakest_kii": result["improvement_area"],
        }

    def get_engagement(self, user_id: int) -> dict[str, Any]:
        response_data = get_user_response_rate(user_id)
        if response_data is None:
            return {
                "user_id": user_id,
                "questions_sent": 0,
                "questions_answered": 0,
                "response_percentage": 0.0,
                "notification_type": "IMPROVEMENT",
            }
        return response_data

    def get_sentiment(self, user_id: int) -> dict[str, Any]:
        eligible_responses = get_eligible_user_qa(user_id, self.db_engine)
        if not eligible_responses:
            return {
                "user_id": user_id,
                "notification_type": "SENTIMENT_QA",
                "next_question": None,
                "selection_status": None,
            }

        question_id = eligible_responses[0]["question_id"]
        selected_rows = [row for row in eligible_responses if row["question_id"] == question_id]
        answers = [{
            "answer_id": row.get("answer_id"),
            "answer": row.get("answer"),
        } for row in selected_rows if row.get("answer_id") is not None]

        return {
            "user_id": user_id,
            "notification_type": "SENTIMENT_QA",
            "next_question": {
                "question_id": question_id,
                "question": selected_rows[0]["question"],
                "answers": answers,
            },
            "selection_status": selected_rows[0].get("selection_status", "UNUSED"),
        }

    def build_notification(
        self,
        user_id: NotificationRequest | int,
        *,
        flow: str | None = None,
        should_send: bool | None = None,
        allow_fallback: bool = True,
    ) -> NotificationProcessingResult | None:
        self.last_skip_reason = None
        self.last_error = None
        self.last_error_type = None
        if isinstance(user_id, NotificationRequest):
            request = user_id
            user_id = request.user_id
            flow = flow or request.flow
            should_send = request.should_send if should_send is None else should_send
        else:
            should_send = True if should_send is None else should_send

        if user_id <= 0:
            raise ValueError("user_id must be greater than zero")

        if flow is None:
            event_type = get_next_manual_notification_for_user(
                user_id,
                db_engine=self.db_engine,
            )
            flow = FLOW_BY_EVENT_TYPE[event_type]

        notification_type = {
            "performance": "VIDEO_RECOMMENDATION",
            "engagement": "SENTIMENT_ENGAGEMENT",
            "sentiment": "SENTIMENT_QA",
        }[flow]

        flow_context = self._build_flow_context(user_id, flow)
        if flow_context is None:
            # SENTIMENT_QA must always be grounded in an application-selected
            # question. Do not send a generic fallback when there is no
            # eligible assigned question to advance the user's cycle.
            if flow == "sentiment":
                return None
            if allow_fallback:
                profile = self._get_user_profile(user_id)
                user_name = profile["user_name"] if profile else f"User {user_id}"
                self.last_skip_reason = self.last_skip_reason or MISSING_REQUIRED_DATA
                notification_data = self._fallback_notification(
                    flow,
                    user_name,
                    {
                        "performance": "your current growth area",
                        "engagement": "your CLAN participation",
                        "sentiment": "your recent responses and workplace reflections",
                    }[flow],
                )
                response = self._make_result(
                    user_id=user_id,
                    notification_type=notification_type,
                    title=notification_data["title"],
                    description=notification_data["description"],
                    reference_id=0,
                    video_popup=None,
                    flow=flow,
                    should_send=should_send,
                    reason=self.last_skip_reason,
                )
                response.reason = self.last_skip_reason
                return response
            return None
        user_name, payload = flow_context
        if flow == "sentiment" and not payload["history_records"]:
            if not allow_fallback:
                return None
            self.last_skip_reason = None
        logger.info("[NOTIFICATION] Step=LLM_GENERATION user_id=%s flow=%s", user_id, flow)
        try:
            notification_data = self._generate_llm_notification(flow, user_name, payload)
        except Exception as exc:
            self.last_skip_reason = LLM_NO_RESPONSE
            self.last_error = str(exc)
            self.last_error_type = type(exc).__name__
            if not _is_terminal_output_error(exc):
                logger.exception(
                    "[NOTIFICATION] ERROR: LLM generation failed for user=%s flow=%s",
                    user_id,
                    flow,
                )
            return None
        if (
            not isinstance(notification_data, dict)
            or not isinstance(notification_data.get("title"), str)
            or not notification_data["title"].strip()
            or not isinstance(notification_data.get("description"), str)
            or not notification_data["description"].strip()
        ):
            self.last_skip_reason = LLM_NO_RESPONSE
            return None

        if flow == "performance":
            video = payload["video"]
            reference_id = int(video["video_id"])
        else:
            reference_id = 0
        video_popup = True if flow == "performance" else None

        notification = self.engine.make_notification(
            user_id=user_id,
            notification_type=notification_type,
            title=notification_data["title"],
            description=notification_data["description"],
            reference_id=reference_id,
            video_popup=video_popup,
        )

        response = NotificationProcessingResult(
            user_id=notification.user_id,
            title=notification.title,
            description=notification.description,
            notification_type=notification.notification_type,
            reference_id=notification.reference_id,
            video_popup=notification.video_popup,
            image=notification.image,
            flow=flow,
            should_send=should_send,
            reason=self.last_skip_reason,
        )

        if should_send:
            if not self.sender.remote_url:
                response.remote_send_status = "skipped"
                response.error = "REMOTE_NOTIFICATION_SEND_URL is not configured"
                response.reason = response.reason or "REMOTE_NOTIFICATION_SEND_URL is not configured"
            else:
                try:
                    remote_response = self.sender.send(
                        user_id=notification.user_id,
                        notification_type=notification.notification_type,
                        title=notification.title,
                        description=notification.description,
                        reference_id=notification.reference_id,
                        video_popup=notification.video_popup,
                        image=notification.image,
                    )
                    response.remote_send_status = "sent"
                    response.remote_send_response = remote_response
                    if flow == "sentiment" and payload.get("history_records"):
                        save_sentiment_notification_history(
                            payload["history_records"],
                            self.db_engine,
                        )
                except Exception as exc:  # pragma: no cover - defensive fallback
                    if not _is_terminal_output_error(exc):
                        logger.exception(
                            "Notification send failed for user=%s flow=%s",
                            user_id,
                            flow,
                        )
                    response.remote_send_status = "failed"
                    response.error = str(exc)
                    response.remote_send_error_type = type(exc).__name__
                    response.reason = response.reason or str(exc)

        return response

    async def build_notification_async(
        self,
        user_id: NotificationRequest | int,
        *,
        flow: str | None = None,
        should_send: bool | None = None,
        allow_fallback: bool = True,
    ) -> NotificationProcessingResult | None:
        self.last_skip_reason = None
        self.last_error = None
        self.last_error_type = None
        if isinstance(user_id, NotificationRequest):
            request = user_id
            user_id = request.user_id
            flow = flow or request.flow
            should_send = request.should_send if should_send is None else should_send
        else:
            should_send = True if should_send is None else should_send

        if user_id <= 0:
            raise ValueError("user_id must be greater than zero")

        if flow is None:
            event_type = get_next_manual_notification_for_user(
                user_id,
                db_engine=self.db_engine,
            )
            flow = FLOW_BY_EVENT_TYPE[event_type]

        notification_type = {
            "performance": "VIDEO_RECOMMENDATION",
            "engagement": "SENTIMENT_ENGAGEMENT",
            "sentiment": "SENTIMENT_QA",
        }[flow]

        flow_context = self._build_flow_context(user_id, flow)
        if flow_context is None:
            if flow == "sentiment":
                return None
            if allow_fallback:
                profile = self._get_user_profile(user_id)
                user_name = profile["user_name"] if profile else f"User {user_id}"
                self.last_skip_reason = self.last_skip_reason or MISSING_REQUIRED_DATA
                notification_data = self._fallback_notification(
                    flow,
                    user_name,
                    {
                        "performance": "your current growth area",
                        "engagement": "your CLAN participation",
                        "sentiment": "your recent responses and workplace reflections",
                    }[flow],
                )
                response = self._make_result(
                    user_id=user_id,
                    notification_type=notification_type,
                    title=notification_data["title"],
                    description=notification_data["description"],
                    reference_id=0,
                    video_popup=None,
                    flow=flow,
                    should_send=should_send,
                    reason=self.last_skip_reason,
                )
                response.reason = self.last_skip_reason
                return response
            return None
        user_name, payload = flow_context
        if flow == "sentiment" and not payload["history_records"]:
            if not allow_fallback:
                return None
            self.last_skip_reason = None
        logger.info("[NOTIFICATION] Step=LLM_GENERATION user_id=%s flow=%s", user_id, flow)
        try:
            notification_data = await self._generate_llm_notification_async(flow, user_name, payload)
        except Exception as exc:
            self.last_skip_reason = LLM_NO_RESPONSE
            self.last_error = str(exc)
            self.last_error_type = type(exc).__name__
            if not _is_terminal_output_error(exc):
                logger.exception(
                    "[NOTIFICATION] ERROR: LLM generation failed for user=%s flow=%s",
                    user_id,
                    flow,
                )
            return None
        if (
            not isinstance(notification_data, dict)
            or not isinstance(notification_data.get("title"), str)
            or not notification_data["title"].strip()
            or not isinstance(notification_data.get("description"), str)
            or not notification_data["description"].strip()
        ):
            self.last_skip_reason = LLM_NO_RESPONSE
            return None

        if flow == "performance":
            video = payload["video"]
            reference_id = int(video["video_id"])
        else:
            reference_id = 0
        video_popup = True if flow == "performance" else None

        notification = self.engine.make_notification(
            user_id=user_id,
            notification_type=notification_type,
            title=notification_data["title"],
            description=notification_data["description"],
            reference_id=reference_id,
            video_popup=video_popup,
        )

        response = NotificationProcessingResult(
            user_id=notification.user_id,
            title=notification.title,
            description=notification.description,
            notification_type=notification.notification_type,
            reference_id=notification.reference_id,
            video_popup=notification.video_popup,
            image=notification.image,
            flow=flow,
            should_send=should_send,
            reason=self.last_skip_reason,
        )

        if should_send:
            if not self.sender.remote_url:
                response.remote_send_status = "skipped"
                response.error = "REMOTE_NOTIFICATION_SEND_URL is not configured"
                response.reason = response.reason or "REMOTE_NOTIFICATION_SEND_URL is not configured"
            else:
                try:
                    remote_response = await self.sender.send_async(
                        user_id=notification.user_id,
                        notification_type=notification.notification_type,
                        title=notification.title,
                        description=notification.description,
                        reference_id=notification.reference_id,
                        video_popup=notification.video_popup,
                        image=notification.image,
                    )
                    response.remote_send_status = "sent"
                    response.remote_send_response = remote_response
                    if flow == "sentiment" and payload.get("history_records"):
                        save_sentiment_notification_history(
                            payload["history_records"],
                            self.db_engine,
                        )
                except Exception as exc:  # pragma: no cover - defensive fallback
                    logger.exception(
                        "Notification send failed for user=%s flow=%s",
                        user_id,
                        flow,
                    )
                    response.remote_send_status = "failed"
                    response.error = str(exc)
                    response.remote_send_error_type = type(exc).__name__
                    response.reason = response.reason or str(exc)

        return response


__all__ = ["NotificationService"]
