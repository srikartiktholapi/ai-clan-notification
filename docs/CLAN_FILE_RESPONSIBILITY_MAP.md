# CLAN Notification File Responsibility Map

This map prioritizes files useful for maintaining the notification pipeline.
“Called by” lists in-repository runtime call sites identified from imports and
function references. External callers and deployment overrides are unknown.

| File | Responsibility | Main functions/classes | Called by | External dependencies | Related tests |
|---|---|---|---|---|---|
| [`app/main.py`](../app/main.py) | FastAPI app, startup/shutdown, endpoints | `lifespan`, `send_notification`, GET handlers | Uvicorn | FastAPI, scheduler module | `test_notification_api.py`, `test_app_scheduler_lifespan.py` |
| [`app/config.py`](../app/config.py) | Environment and `.env` settings, startup validation | `load_settings`, `validate_configuration` | App startup, sender, LLM client | python-dotenv, environment | `test_config.py` |
| [`app/constants.py`](../app/constants.py) | Event-to-flow and next-event mappings | `FLOW_BY_EVENT_TYPE`, `NEXT_NOTIFICATION_BY_EVENT_TYPE` | Service, daily runner, notification repository | — | `test_notification_cycle.py`, service tests |
| [`scripts/apscheduler_runner.py`](../scripts/apscheduler_runner.py) | Daily APScheduler lifecycle and trigger | `load_schedule_config`, `create_scheduler`, `start_scheduler`, `shutdown_scheduler`, `main` | FastAPI lifespan or direct module entry point | APScheduler, env | `test_apscheduler_runner.py`, `test_app_scheduler_lifespan.py` |
| [`scripts/run_daily.py`](../scripts/run_daily.py) | User collection, per-user selection/delivery, status logging | `_collect_users`, `_process_user_async`, `run_scheduler_async`, `main` | APScheduler job; direct script execution | SQLAlchemy, asyncio, service, remote sender/LLM through service | `test_retry_and_scheduler.py`, `test_notifications_service.py` |
| [`app/notifications/service.py`](../app/notifications/service.py) | Orchestrates profile, flow context, prompt, payload, delivery | `NotificationService`, `_build_flow_context`, `build_notification`, `build_notification_async` | API and `run_daily.py` | Repositories, OpenAI client, sender, SQLAlchemy | `test_notifications_service.py`, `test_notification_api.py` |
| [`app/notifications/models.py`](../app/notifications/models.py) | Request, notification, and result schemas | `NotificationSendRequest`, `NotificationRequest`, `Notification`, `NotificationProcessingResult` | API, service, scheduler | Pydantic | `test_notification.py`, API/service tests |
| [`app/notifications/engine.py`](../app/notifications/engine.py) | Builds validated notification object | `NotificationEngine.make_notification` | Service | Pydantic model | `test_notification.py` |
| [`app/notifications/sender.py`](../app/notifications/sender.py) | Sync/async POST client and retry logic | `NotificationSender.send`, `send_async`, `_prepare_payload` | Service sync/async paths | requests, httpx, remote URL | `test_retry_and_scheduler.py`, API/service tests |
| [`app/database/connection.py`](../app/database/connection.py) | SQLAlchemy engine initialization | module-level `engine` | Database repositories and modules | SQLAlchemy, database driver, `DATABASE_URL` | Most DB tests mock; some tests use engine directly |
| [`app/database/notification_repository.py`](../app/database/notification_repository.py) | History selector, date duplicate check, legacy local insert helpers | `get_next_notification_for_user`, `get_next_manual_notification_for_user`, `has_notification_for_user_on_date`, `schedule_next_notification_for_user`, `insert_notification` | Daily runner/service; scheduling helper calls insert | PostgreSQL | `test_notification_cycle.py`; scheduler/API tripwire tests |
| [`app/database/user_repository.py`](../app/database/user_repository.py) | User/profile and KII/video helper queries | `get_user`, `get_last_7_days_kii_performance`; other helper functions | Service and active performance path | PostgreSQL | `test_user_repository.py`, service/performance tests |
| [`app/performance/performance.py`](../app/performance/performance.py) | Active fixed-ID performance calculation and separate helper chain | `calculate_performance`, `_get_performance_rows`, `get_performance_details`, `get_weakest_performance` | Service; recommendation helper uses `calculate_performance` | PostgreSQL via user repository or module engine | `test_performance.py`, `test_recommendation.py` |
| [`app/recommendation/recommendation.py`](../app/recommendation/recommendation.py) | Query embedding and video recommendation orchestration | `embed_text`, `recommend_video`, `recommend_for_user` | Service calls `recommend_video`; `recommend_for_user` has no application caller found | sentence-transformers, PostgreSQL search helper | `test_recommendation.py`, service tests |
| [`app/database/vector_search.py`](../app/database/vector_search.py) | Active KII/language/watched-history search and separate query-points implementation | `search_videos`, `query_points`, `SearchResult` | Active `recommend_video` calls `search_videos`; no internal caller found for `query_points` | PostgreSQL, pgvector | `test_vector_search.py`, recommendation tests |
| [`app/sentiment/sentiment.py`](../app/sentiment/sentiment.py) | Engagement rate, Q/A selection/grouping, sentiment-history persistence | `get_user_response_rate`, `get_eligible_user_qa`, `prepare_user_qa`, `save_sentiment_notification_history` | Service; helper functions call one another | PostgreSQL | `test_sentiment.py`, service/API tests |
| [`app/llm/llm_client.py`](../app/llm/llm_client.py) | Active OpenAI Responses API client, retries, output validation | `NotificationGenerator`, retry/client helpers | Service; prompt modules import selected helpers | OpenAI Python SDK | `test_retry_and_scheduler.py`, `test_config.py` |
| [`app/llm/generate_performance_notification.py`](../app/llm/generate_performance_notification.py) | Performance/video prompt and standalone generator | `build_performance_notification_prompt`, `validate_performance_notification` | Active service calls prompt builder | `llm_client` | `test_llm.py`, service tests |
| [`app/llm/generate_engagement_sentiment_notification.py`](../app/llm/generate_engagement_sentiment_notification.py) | Engagement sentiment prompt and standalone generator | prompt builder, validator, generator | Active service calls prompt builder | `llm_client` | `test_llm.py`, service tests |
| [`app/llm/generate_qa_sentiment_notification.py`](../app/llm/generate_qa_sentiment_notification.py) | Answered and unanswered Q/A prompts, validation | `build_qa_sentiment_notification_prompt`, `build_no_qa_sentiment_notification_prompt` | Active service calls prompt builders | `llm_client` | `test_llm.py`, service tests |
| [`app/llm/notification_generator.py`](../app/llm/notification_generator.py) | Separate overlapping generator implementation | `NotificationGenerator`, standalone generator functions | No active app caller found; tests import it | OpenAI SDK | `test_llm.py`; logger name in `run_daily.py` |
| [`tests/test_notifications_service.py`](../tests/test_notifications_service.py) | Mocked service flow, fallback, language, delivery, concurrency tests | service tests | pytest | Test fakes/mocks | Self |
| [`tests/test_retry_and_scheduler.py`](../tests/test_retry_and_scheduler.py) | Retry, sender, and scheduler behavior | retry/scheduler tests | pytest | Monkeypatches/fakes | Self |
| [`tests/test_performance.py`](../tests/test_performance.py) | DB-backed expected calculations and API assertions | `test_real_performance`, `test_performance_api`, `test_performance_function_matches_api` | pytest | **Direct configured DB engine queries** | Self; only run with isolated DB |
| [`tests/test_recommendation.py`](../tests/test_recommendation.py) | Embedding/recommendation tests plus language mapping query | recommendation tests | pytest | One test uses **direct configured DB engine query** | Self; isolate/skip that DB test safely |
| [`README.md`](../README.md) | Setup/API/testing guidance | — | Developers | env/config | No docs tests found |
| [`Dockerfile`](../Dockerfile) | Container build and Uvicorn launch | Uvicorn command, model bake-in | Deployment | Python image, pip, model download during build | No Docker integration test found |
| [`scripts/allow_null_sentiment_notification_history_answer_ids.sql`](../scripts/allow_null_sentiment_notification_history_answer_ids.sql) | Migration making sentiment history answer references nullable | `ALTER TABLE` | Manual/deployment process not established | PostgreSQL | No migration test found |
| [`requirements.txt`](../requirements.txt), [`requirements-dev.txt`](../requirements-dev.txt), [`pyproject.toml`](../pyproject.toml) | Dependencies and pytest configuration | — | Install/test tooling | Python packages | pytest suite |

## Important non-findings and caveats

- No tracked CI workflow, compose file, cron shell script, deployment manifest,
  or `scripts/run_daily.sh`/`scripts/install.sh` was found.
- No embedding-ingestion script or remote receiver implementation was found.
- `notification_repository.insert_notification()` is not called by the active
  API or scheduled send path. The repository's receiver persistence boundary
  cannot be proven from this codebase.
- The application uses `app.llm.llm_client.NotificationGenerator`. The
  similarly named `app.llm.notification_generator` is imported by tests but
  not by the active service.
- Test results are not encoded as current guarantees. The last observed suite
  had 155 passes and 12 failures; scheduler tests include expectations that do
  not match current runner behavior.
- A full test run is unsafe until the configured DB is verified as isolated:
  `test_performance.py` and one `test_recommendation.py` test issue direct
  SQLAlchemy reads.
