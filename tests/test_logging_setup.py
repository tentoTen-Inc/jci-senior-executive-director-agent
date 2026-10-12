"""構造化ログ（障害通知の前提: 深刻度が読めること・例外が1件にまとまること）。"""
import json
import logging

from app.logging_setup import CloudLoggingFormatter, configure_logging


def _record(level, msg, exc_info=None):
    return logging.LogRecord("jci-agent.test", level, __file__, 1, msg, None, exc_info)


def test_severity_is_set():
    fmt = CloudLoggingFormatter()
    for level, name in [(logging.INFO, "INFO"), (logging.WARNING, "WARNING"),
                        (logging.ERROR, "ERROR")]:
        entry = json.loads(fmt.format(_record(level, "メッセージ")))
        assert entry["severity"] == name
        assert entry["message"] == "メッセージ"  # 日本語をエスケープしない
        assert entry["logger"] == "jci-agent.test"


def test_exception_is_one_entry():
    try:
        raise RuntimeError("Gemini API 500: internal\n詳細")
    except RuntimeError:
        import sys
        line = CloudLoggingFormatter().format(_record(logging.ERROR, "失敗", sys.exc_info()))
    assert "\n" not in line  # 1行の JSON（Cloud Logging で行ごとに分割されない）
    entry = json.loads(line)
    assert entry["severity"] == "ERROR"
    assert "Traceback" in entry["message"] and "Gemini API 500" in entry["message"]


def test_local_keeps_plain_format(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    configure_logging()  # ローカルでは何も壊さない
    assert not any(isinstance(h.formatter, CloudLoggingFormatter)
                   for h in logging.getLogger().handlers)


def test_cloud_run_uses_json(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "jci-sed-agent")
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        configure_logging()
        assert isinstance(root.handlers[0].formatter, CloudLoggingFormatter)
        assert logging.getLogger("uvicorn.error").propagate
    finally:
        root.handlers[:] = saved
