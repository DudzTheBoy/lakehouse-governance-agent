"""Tests for source-documentation retrieval.

Retrieval is lexical on purpose, which makes it testable: given a table's vocabulary,
a specific passage either wins or it does not, and the test can say which. That is
the property being defended here -- a vector index would make these assertions
approximate.

The retrieval width regression is the one that matters. A first version returned
three chunks, which was not enough for a table spanning several sections of its
system's documentation, and a Genesys duration column kept being described as a
timestamp because the section defining durations never made the cut.
"""

import pytest

from docs_index import DocsIndex, tokenize


class TestTokenize:
    def test_splits_camel_case(self):
        # `wrapupCode` in a column has to match `wrapup code` in prose.
        assert "wrapup" in tokenize("wrapupCode")
        assert "code" in tokenize("wrapupCode")

    def test_splits_snake_case(self):
        assert set(tokenize("l_returnflag")) >= {"returnflag"}

    def test_drops_stopwords_and_single_characters(self):
        assert tokenize("the a of for") == []

    def test_lowercases(self):
        assert tokenize("TAcw") == tokenize("tacw")


@pytest.fixture(scope="module")
def index():
    return DocsIndex()


class TestSystemMapping:
    @pytest.mark.parametrize(
        "schema, table, expected",
        [
            ("raw_genesys", "conversation_details", "genesys_cloud"),
            ("tpch", "lineitem", "tpch_spec"),
            ("staging", "cad_gen_2021", "legacy_crm"),
            ("raw", "clientes", "seguracore"),
            ("silver", "apolices_curated", "seguracore"),
        ],
    )
    def test_patterns_resolve(self, index, schema, table, expected):
        assert index.system_for(schema, table) == expected

    def test_unmapped_schema_returns_nothing(self, index):
        assert index.system_for("unmapped_schema", "whatever") is None

    def test_unmapped_table_retrieves_nothing(self, index):
        assert index.retrieve("unmapped_schema", "whatever", ["a", "b"]) is None


class TestRetrieval:
    def test_coded_values_passage_reaches_a_tpch_table(self, index):
        # The rules for l_returnflag and l_linestatus live in one section. If it does
        # not come back, the descriptions cannot be right no matter what the model does.
        result = index.retrieve(
            "tpch", "lineitem",
            ["l_orderkey", "l_returnflag", "l_linestatus", "l_shipmode", "l_extendedprice"],
        )
        assert result is not None
        assert any("Coded column values" in c for c in result["citations"])
        assert "L_LINESTATUS" in result["context"]

    def test_duration_metrics_reach_a_wide_genesys_table(self, index):
        # The regression: this table spans identifiers, attributes and durations, and
        # three chunks was not enough to carry all three.
        result = index.retrieve(
            "raw_genesys", "conversation_details",
            ["conversationId", "participantId", "sessionId", "mediaType", "direction",
             "purpose", "queueId", "wrapUpCode", "tAnswered", "tTalk", "tHeld", "tAcw"],
        )
        assert result is not None
        assert any("Duration metrics" in c for c in result["citations"])
        assert "millisecond" in result["context"].lower()

    def test_matched_terms_are_reported_for_audit(self, index):
        # Provenance is the reason retrieval is lexical: a description has to be able
        # to name the passage and the terms that selected it.
        result = index.retrieve("tpch", "orders", ["o_orderkey", "o_orderstatus", "o_totalprice"])
        assert result is not None
        assert result["matched_terms"]
        assert result["citations"]

    def test_context_stays_within_budget(self, index):
        from docs_index import MAX_CONTEXT_CHARS

        result = index.retrieve(
            "tpch", "lineitem",
            ["l_orderkey", "l_partkey", "l_suppkey", "l_returnflag", "l_linestatus"],
        )
        assert result is not None
        assert len(result["context"]) <= MAX_CONTEXT_CHARS

