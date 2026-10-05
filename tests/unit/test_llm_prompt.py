import copy
import dataclasses
import json

import pytest

from submissions_checker.services.llm_grading.judge import (
    GradingRequest,
    JudgeCriterion,
    JudgeError,
    WorkFile,
)
from submissions_checker.services.llm_grading.prompt import (
    SYSTEM_PROMPT,
    build_prefix,
    build_prompt,
    parse_result,
    result_schema,
)

CRIT = [
    JudgeCriterion("report", "Звіт", 5, "Є висновки"),
    JudgeCriterion("code", "Код", 3, "Сервер працює"),
]
REQ = GradingRequest(
    task="T", instructions="", criteria=CRIT, files=[WorkFile("a.pdf", b"%PDF", "application/pdf")]
)
GOOD = {
    "criteria": {
        "report": {"points": 4, "justification": "j", "evidence": "e"},
        "code": {"points": 3, "justification": "j", "evidence": ""},
    },
    "comment": "c",
}


def _parse(obj):
    return parse_result(json.dumps(obj), CRIT, "p", "m")


def test_prefix_identical_across_works():
    other = dataclasses.replace(REQ, files=[WorkFile("b.docx", b"x", "application/octet-stream")])
    assert build_prefix(REQ) == build_prefix(other)
    assert build_prompt(REQ) != build_prompt(other)


def test_prompt_lists_manifest_and_criteria():
    p = build_prompt(REQ)
    assert "- a.pdf (application/pdf, 4 bytes)" in p
    assert "Є висновки" in p and "max points: 5" in p


def test_schema_shape():
    s = result_schema(CRIT)
    assert s["properties"]["criteria"]["required"] == ["report", "code"]
    assert (
        s["properties"]["criteria"]["properties"]["report"]["properties"]["points"]["maximum"] == 5
    )


def test_parse_ok():
    r = _parse(GOOD)
    assert r.criteria["report"].points == 4
    assert (r.provider, r.model, r.usage) == ("p", "m", {})


def test_parse_extracts_fenced_json():
    raw = "Ось:\n```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_result(raw, CRIT, "p", "m").comment == "c"


def test_parse_extracts_json_embedded_in_prose():
    raw = "Result {not json} follows: " + json.dumps(GOOD) + " thanks"
    assert parse_result(raw, CRIT, "p", "m").criteria["code"].points == 3


@pytest.mark.parametrize("raw", ["", "no json here", "[1, 2]"])
def test_parse_rejects_no_object(raw):
    with pytest.raises(JudgeError):
        parse_result(raw, CRIT, "p", "m")


def test_parse_rejects_out_of_range():
    bad = copy.deepcopy(GOOD)
    bad["criteria"]["code"]["points"] = 4
    with pytest.raises(JudgeError, match="code"):
        _parse(bad)
    bad["criteria"]["code"]["points"] = -1
    with pytest.raises(JudgeError, match="code"):
        _parse(bad)


def test_parse_rejects_missing_criterion():
    bad = copy.deepcopy(GOOD)
    del bad["criteria"]["code"]
    with pytest.raises(JudgeError, match="code"):
        _parse(bad)


@pytest.mark.parametrize("points", [True, 2.0, 2.5, "3", None])
def test_parse_rejects_bool_and_float_points(points):
    bad = copy.deepcopy(GOOD)
    bad["criteria"]["code"]["points"] = points
    with pytest.raises(JudgeError, match="code"):
        _parse(bad)


@pytest.mark.parametrize("value", ["", "   ", None, 3])
def test_parse_rejects_empty_justification(value):
    bad = copy.deepcopy(GOOD)
    bad["criteria"]["report"]["justification"] = value
    with pytest.raises(JudgeError, match="report"):
        _parse(bad)


def test_parse_rejects_missing_evidence_and_comment():
    bad = copy.deepcopy(GOOD)
    del bad["criteria"]["report"]["evidence"]
    with pytest.raises(JudgeError, match="evidence"):
        _parse(bad)
    bad = copy.deepcopy(GOOD)
    del bad["comment"]
    with pytest.raises(JudgeError, match="comment"):
        _parse(bad)


def test_parse_drops_unknown_keys():
    extra = copy.deepcopy(GOOD)
    extra["criteria"]["bonus"] = {"points": 99, "justification": "x", "evidence": ""}
    extra["criteria"]["code"]["note"] = "x"
    r = _parse(extra)
    assert set(r.criteria) == {"report", "code"}


def test_system_prompt_marks_work_as_data():
    low = SYSTEM_PROMPT.lower()
    assert "data" in low and "instruction" in low
    assert "Ukrainian" in SYSTEM_PROMPT and "300" in SYSTEM_PROMPT
