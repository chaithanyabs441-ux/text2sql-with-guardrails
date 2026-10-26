"""Offline tests for evaluation result comparison and report output."""

import csv
import json

import pandas as pd
from eval.run_eval import EvaluationRunner, result_sets_equal, write_reports

from t2s.generation.generator import SQLGenerationResponse


class StubLLM:
    def get_usage(self):
        return {
            "cache_hit": False,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "estimated_cost_usd": 0.001,
        }


class StubGenerator:
    llm = StubLLM()

    def generate(self, question, strategy, database_error=None):
        if question == "unknown":
            return (
                SQLGenerationResponse(
                    sql="",
                    explanation="Not present in the schema.",
                    tables_used=[],
                    self_reported_confidence=0.9,
                    answerable=False,
                    ambiguity_flag=False,
                ),
                "",
            )
        return (
            SQLGenerationResponse(
                sql="SELECT id FROM customers ORDER BY id",
                explanation="Select customer identifiers.",
                tables_used=["customers"],
                self_reported_confidence=0.9,
                answerable=True,
                ambiguity_flag=False,
            ),
            "SELECT id FROM customers ORDER BY id LIMIT 1000",
        )


class StubSandbox:
    def execute(self, sql):
        rows = [2, 1] if "gold" in sql else [1, 2]
        return pd.DataFrame({"id": rows}), 1.0, None


def test_result_comparison_ignores_row_order_and_column_names():
    left = pd.DataFrame({"gold_name": [1, 2, 2]})
    right = pd.DataFrame({"generated_name": [2, 1, 2]})

    assert result_sets_equal(left, right)


def test_runner_writes_raw_jsonl_and_strategy_metrics(tmp_path):
    questions = [
        {
            "id": "g001",
            "question": "known",
            "expected_answerable": True,
            "expected_ambiguity": False,
            "category": "lookup",
            "gold_sql": "SELECT id FROM customers /* gold */",
        },
        {
            "id": "g002",
            "question": "unknown",
            "expected_answerable": False,
            "expected_ambiguity": False,
            "gold_sql": None,
        },
    ]
    runner = EvaluationRunner(generator=StubGenerator(), sandbox=StubSandbox())

    raw_records, metrics = runner.run(questions, ("zero_shot",))
    raw_path, csv_path = write_reports(tmp_path, raw_records, metrics)
    metric_rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))

    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 2
    assert json.loads(raw_path.read_text(encoding="utf-8").splitlines()[0])["result_match"]
    assert metric_rows[0]["valid_sql_rate"] == "0.5"
    assert metric_rows[0]["execution_accuracy_all_gold"] == "1.0"
    assert metric_rows[0]["execution_accuracy_comparable"] == "1.0"
    category_rows = list(csv.DictReader((tmp_path / "category_metrics.csv").open(encoding="utf-8")))
    assert category_rows[0]["category"] == "lookup"


def test_selfcorrect_strategy_retries_database_error_at_most_twice():
    class RetryGenerator(StubGenerator):
        def __init__(self):
            self.database_errors = []

        def generate(self, question, strategy, database_error=None):
            self.database_errors.append(database_error)
            response, sql = super().generate(question, strategy)
            return response, sql

    class RetrySandbox:
        def __init__(self):
            self.calls = 0

        def execute(self, sql):
            self.calls += 1
            if self.calls <= 2:
                return pd.DataFrame(), 1.0, "syntax error near SELECT"
            return pd.DataFrame({"id": [1, 2]}), 1.0, None

    generator = RetryGenerator()
    runner = EvaluationRunner(generator=generator, sandbox=RetrySandbox())
    question = {
        "id": "g001",
        "question": "known",
        "expected_answerable": True,
        "expected_ambiguity": False,
        "gold_sql": "SELECT id FROM customers /* gold */",
    }

    records, _ = runner.run([question], ("schema_glossary_selfcorrect",))

    assert generator.database_errors == [None, "Execution error: syntax error near SELECT"]
    assert records[0]["retry_count"] == 1
    assert len(records[0]["attempts"]) == 2
    assert records[0]["attempts"][0]["retry_reason"].startswith("Execution error:")


def test_all_gold_accuracy_counts_failed_generation_as_wrong(tmp_path):
    questions = [
        {
            "id": "g001",
            "question": "known",
            "category": "lookup",
            "gold_sql": "SELECT id FROM customers /* gold */",
        },
        {
            "id": "g002",
            "question": "unknown",
            "category": "unanswerable_but_gold_present",
            "gold_sql": "SELECT id FROM customers /* gold */",
        },
    ]
    runner = EvaluationRunner(generator=StubGenerator(), sandbox=StubSandbox())

    raw_records, metrics = runner.run(
        questions, ("zero_shot", "schema_glossary", "schema_glossary_selfcorrect")
    )
    _, csv_path = write_reports(tmp_path, raw_records, metrics)
    metric_rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    metric = next(row for row in metric_rows if row["strategy"] == "zero_shot")
    selfcorrect = next(
        row for row in metric_rows if row["strategy"] == "schema_glossary_selfcorrect"
    )
    categories = list(csv.DictReader((tmp_path / "category_metrics.csv").open(encoding="utf-8")))

    assert metric["gold_sql_questions"] == "2"
    assert metric["correct_result_pairs"] == "1"
    assert metric["execution_accuracy_all_gold"] == "0.5"
    assert metric["comparable_pairs"] == "1"
    assert metric["execution_accuracy_comparable"] == "1.0"
    failed_category = next(
        row for row in categories if row["category"] == "unanswerable_but_gold_present"
    )
    assert failed_category["execution_accuracy_all_gold"] == "0.0"
    assert failed_category["comparable_pairs"] == "0"
    assert selfcorrect["all_gold_delta_vs_schema_glossary"] == "0.0"
    assert selfcorrect["comparable_delta_vs_schema_glossary"] == "0.0"
    assert raw_records[0]["attempts"][0]["cache_status"] == "live"


def test_selfcorrect_retries_empty_results_and_logs_each_attempt():
    class FeedbackGenerator(StubGenerator):
        def __init__(self):
            self.feedback = []

        def generate(self, question, strategy, database_error=None):
            self.feedback.append(database_error)
            return super().generate("known", strategy)

    class EmptyThenRowsSandbox:
        def __init__(self):
            self.calls = 0

        def execute(self, sql):
            self.calls += 1
            if self.calls == 1:  # gold SQL
                return pd.DataFrame({"id": [1, 2]}), 1.0, None
            if self.calls == 2:
                return pd.DataFrame({"id": []}), 1.0, None
            return pd.DataFrame({"id": [1, 2]}), 1.0, None

    generator = FeedbackGenerator()
    runner = EvaluationRunner(generator=generator, sandbox=EmptyThenRowsSandbox())
    question = {
        "id": "g001",
        "question": "known",
        "category": "lookup",
        "gold_sql": "SELECT id FROM customers /* gold */",
    }

    records, _ = runner.run([question], ("schema_glossary_selfcorrect",))

    assert records[0]["retry_count"] == 1
    assert len(records[0]["attempts"]) == 2
    assert "zero rows" in generator.feedback[1]
    assert records[0]["attempts"][0]["sanity_warnings"]
    assert records[0]["attempts"][0]["retry_reason"]
    assert records[0]["attempts"][1]["retry_reason"] is None


def test_selfcorrect_retries_sanity_warnings():
    class FeedbackGenerator(StubGenerator):
        def __init__(self):
            self.feedback = []

        def generate(self, question, strategy, database_error=None):
            self.feedback.append(database_error)
            return super().generate("known", strategy)

    class WarningThenRowsSandbox:
        def __init__(self):
            self.calls = 0

        def execute(self, sql):
            self.calls += 1
            if self.calls == 1:  # gold SQL
                return pd.DataFrame({"id": [1, 2]}), 1.0, None
            if self.calls == 2:
                return pd.DataFrame({"revenue": [-5, -10]}), 1.0, None
            return pd.DataFrame({"id": [1, 2]}), 1.0, None

    generator = FeedbackGenerator()
    runner = EvaluationRunner(generator=generator, sandbox=WarningThenRowsSandbox())
    question = {
        "id": "g001",
        "question": "known",
        "category": "lookup",
        "gold_sql": "SELECT id FROM customers /* gold */",
    }

    records, _ = runner.run([question], ("schema_glossary_selfcorrect",))

    assert records[0]["retry_count"] == 1
    assert "mostly negative" in records[0]["attempts"][0]["retry_reason"]
    assert "mostly negative" in generator.feedback[1]


def test_selfcorrect_caps_retries_at_two_for_persistent_sanity_warnings():
    class EmptySandbox:
        def __init__(self):
            self.calls = 0

        def execute(self, sql):
            self.calls += 1
            if self.calls == 1:  # gold SQL
                return pd.DataFrame({"id": [1]}), 1.0, None
            return pd.DataFrame({"id": []}), 1.0, None

    runner = EvaluationRunner(generator=StubGenerator(), sandbox=EmptySandbox())
    question = {
        "id": "g001",
        "question": "known",
        "category": "lookup",
        "gold_sql": "SELECT id FROM customers /* gold */",
    }

    records, _ = runner.run([question], ("schema_glossary_selfcorrect",))

    assert records[0]["retry_count"] == 2
    assert len(records[0]["attempts"]) == 3
    assert all(attempt["retry_reason"] for attempt in records[0]["attempts"][:2])
