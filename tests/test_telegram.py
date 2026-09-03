import pytest

from us_earnings_monitor.telegram import send_report


class Response:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": 42}}


class Session:
    def __init__(self):
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        return Response()


def test_telegram_delivery_checks_api_success():
    session = Session()
    result = send_report("報告", token="token", chat_id="chat", parse_mode="HTML", session=session)
    assert result["ok"] is True
    assert session.calls == 1


class RejectedResponse(Response):
    def json(self):
        return {"ok": False, "description": "chat not found"}


class RejectedSession(Session):
    def post(self, url, **kwargs):
        self.calls += 1
        return RejectedResponse()


def test_telegram_api_rejection_is_not_marked_as_sent():
    with pytest.raises(RuntimeError, match="chat not found"):
        send_report("報告", token="token", chat_id="chat", session=RejectedSession())
