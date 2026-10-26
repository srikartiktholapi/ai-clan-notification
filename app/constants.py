from app.notifications.models import FlowName


FLOW_BY_EVENT_TYPE: dict[str, FlowName] = {
    "VIDEO_RECOMMENDATION": "performance",
    "SENTIMENT_ENGAGEMENT": "engagement",
    "SENTIMENT_QA": "sentiment",
}
NEXT_NOTIFICATION_BY_EVENT_TYPE = {
    "VIDEO_RECOMMENDATION": "SENTIMENT_ENGAGEMENT",
    "SENTIMENT_ENGAGEMENT": "SENTIMENT_QA",
    "SENTIMENT_QA": "VIDEO_RECOMMENDATION",
}
