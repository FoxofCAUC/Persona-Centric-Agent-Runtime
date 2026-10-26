"""Persisted metadata-only state transitions and measured, non-estimated durations."""
import time

EVENTS = {"accepted", "queued", "retry_queued", "provider_started", "first_output", "generated",
          "persisted", "failed", "interrupted", "cancelled", "delivery_started", "delivery_confirmed",
          "delivery_uncertain", "delivery_failed"}


def record(conn, turn_id, event, attempt_id=None):
    if event not in EVENTS:
        raise ValueError("Unknown progress event")
    conn.execute("INSERT INTO request_activity (turn_id,attempt_id,event,at_epoch) VALUES (?,?,?,?)",
                 (turn_id, attempt_id, event, time.time()))


def progress(conn, turn):
    rows = [dict(row) for row in conn.execute(
        "SELECT seq,attempt_id,event,at_epoch FROM request_activity WHERE turn_id=? ORDER BY seq", (turn["id"],))]
    # Retried generations get separate timing windows; do not overwrite history.
    start = max((i for i, row in enumerate(rows) if row["event"] == "retry_queued"), default=0)
    latest = {row["event"]: row["at_epoch"] for row in rows[start:]}
    accepted = next((r["at_epoch"] for r in rows if r["event"] == "accepted"), None)
    def duration(a, b):
        if a is None or b is None or b < a:
            return None
        return round(b-a, 4)
    phase = turn["status"]
    if turn["cancelled_at"]:
        phase = "cancelled"
    elif phase == "completed" and turn.get("delivery"):
        phase = {"pending": "awaiting_delivery", "sending": "delivering", "sent": "completed",
                 "uncertain": "delivery_uncertain", "failed": "delivery_failed"}[turn["delivery"]]
    queued = latest.get("retry_queued", latest.get("queued"))
    return {"phase": phase, "events": rows, "timings": {
        "queue_seconds": duration(queued, latest.get("provider_started")),
        "first_output_seconds": duration(latest.get("provider_started"), latest.get("first_output")),
        "provider_seconds": duration(latest.get("provider_started"), latest.get("generated", latest.get("failed"))),
        "persist_seconds": duration(latest.get("generated"), latest.get("persisted")),
        "delivery_seconds": duration(latest.get("delivery_started"), latest.get("delivery_confirmed")),
        "total_seconds": duration(accepted, latest.get("delivery_confirmed", latest.get("persisted"))),
    }, "recovery": recovery(phase, turn.get("error_code"))}


def recovery(phase, error=None):
    if phase == "cancelled":
        return "已取消排队；没有为此次取消开始新的模型调用。需要时可显式重试。"
    if phase == "delivery_uncertain":
        return "模型已生成，但平台送达结果不确定；先核对 QQ 历史，不自动补发或重新生成。"
    if phase in ("awaiting_delivery", "delivering"):
        return "模型已生成；等待原投递流程，恢复连接不会再次调用模型。"
    if phase in ("failed", "interrupted"):
        if error == "hourly_limit_reached":
            return "调用额度已用完；等待额度恢复后再显式重试。"
        if error in ("provider_http_401", "provider_http_403"):
            return "核对模型密钥和权限，再显式重试原请求。"
        if error == "provider_http_429":
            return "提供商限流；稍后显式重试，不连续点击。"
        if error == "context_limit_exceeded":
            return "检查输入长度或新建会话；不要反复重试同一超长上下文。"
        return "用户消息已保留；检查错误或连接后，可显式重试原请求，不自动调用。"
    return ""
