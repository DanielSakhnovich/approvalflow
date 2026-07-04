import json
import logging

from afcommon.logging import correlation_id_var, setup_json_logging


def test_log_line_is_json_with_required_fields(capsys):
    setup_json_logging("test-svc")
    correlation_id_var.set("corr-123")
    logging.getLogger(__name__).info("hello")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["service"] == "test-svc"
    assert entry["correlation_id"] == "corr-123"
    assert entry["level"] == "INFO"
    assert entry["message"] == "hello"
    assert "timestamp" in entry and "invoice_id" in entry


def test_default_correlation_id_is_dash(capsys):
    setup_json_logging("test-svc")
    correlation_id_var.set("-")
    logging.getLogger(__name__).warning("no context")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["correlation_id"] == "-"


def test_exception_logging_includes_traceback_in_exception_field(capsys):
    setup_json_logging("test-svc")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger(__name__).exception("something failed")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "exception" in entry
    assert "ValueError: boom" in entry["exception"]
    assert "Traceback" in entry["exception"]
