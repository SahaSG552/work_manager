"""Tests for scanner — _check_filter, _evaluate_chain, _rule_to_chain."""

import pytest
from app.scanner import _check_filter, _evaluate_chain, _rule_to_chain


# ── Sticker map for tests ──────────────────────────────────────────

STICKER_MAP = {
    "abc123": {"sticker": "Фасады", "state": "ORWOOD"},
    "def456": {"sticker": "Статус", "state": "В работе"},
    "ghi789": {"sticker": "Фасады", "state": "GRANDE"},
}

SAMPLE_BOARD = {"id": "board-1", "title": "Test Board"}


# ═══════════════════════════════════════════════════════════════
# _check_filter — individual filter conditions
# ═══════════════════════════════════════════════════════════════

class TestCheckFilterBoard:
    def test_board_eq_match(self):
        task = {"columnId": "col-1"}
        cond = {"field": "board", "op": "eq", "value": "board-1"}
        assert _check_filter(cond, task, SAMPLE_BOARD, STICKER_MAP) is True

    def test_board_eq_no_match(self):
        task = {"columnId": "col-1"}
        cond = {"field": "board", "op": "eq", "value": "board-2"}
        assert _check_filter(cond, task, SAMPLE_BOARD, STICKER_MAP) is False

    def test_board_no_board_info(self):
        task = {"columnId": "col-1"}
        cond = {"field": "board", "op": "eq", "value": "board-1"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False


class TestCheckFilterColumn:
    def test_column_eq(self):
        task = {"columnId": "col-1"}
        cond = {"field": "column", "op": "eq", "value": "col-1"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_column_no_match(self):
        task = {"columnId": "col-1"}
        cond = {"field": "column", "op": "eq", "value": "col-2"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False

    def test_column_any(self):
        task = {"columnId": "col-1"}
        cond = {"field": "column", "op": "any", "value": ""}
        assert _check_filter(cond, task, None, STICKER_MAP) is True


class TestCheckFilterTitle:
    def test_title_contains(self):
        task = {"title": "ЛКПФ1006обрА5 Фасад"}
        cond = {"field": "title", "op": "contains", "value": "фасад"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_title_contains_no_match(self):
        task = {"title": "ЛКПФ1006обрА5"}
        cond = {"field": "title", "op": "contains", "value": "орвуд"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False

    def test_title_eq(self):
        task = {"title": "ЧМ-1102"}
        cond = {"field": "title", "op": "eq", "value": "чм-1102"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True


class TestCheckFilterSticker:
    def test_sticker_eq(self):
        task = {"stickers": {"uuid1": "abc123"}}
        cond = {"field": "sticker", "op": "eq", "value": "abc123"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_sticker_no_match(self):
        task = {"stickers": {"uuid1": "abc123"}}
        cond = {"field": "sticker", "op": "eq", "value": "xyz999"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False


class TestCheckFilterStickerName:
    def test_sticker_name_contains(self):
        task = {"stickers": {"uuid1": "abc123"}}
        cond = {"field": "sticker_name", "op": "contains", "value": "ORWOOD"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_sticker_name_no_match(self):
        task = {"stickers": {"uuid1": "abc123"}}
        cond = {"field": "sticker_name", "op": "contains", "value": "GRANDE"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False


class TestCheckFilterAssigned:
    def test_assigned_match(self):
        task = {"assigned": ["user-1", "user-2"]}
        cond = {"field": "assigned", "op": "eq", "value": "user-1"}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_assigned_list_value(self):
        task = {"assigned": ["user-1", "user-2"]}
        cond = {"field": "assigned", "op": "eq", "value": ["user-1"]}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_assigned_no_match(self):
        task = {"assigned": ["user-1"]}
        cond = {"field": "assigned", "op": "eq", "value": "user-3"}
        assert _check_filter(cond, task, None, STICKER_MAP) is False


class TestCheckFilterDone:
    def test_done_true(self):
        task = {"completed": True}
        cond = {"field": "done", "op": "eq", "value": True}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_done_false(self):
        task = {"completed": False}
        cond = {"field": "done", "op": "eq", "value": False}
        assert _check_filter(cond, task, None, STICKER_MAP) is True

    def test_done_mismatch(self):
        """Task done=True but filter wants undone."""
        task = {"completed": True}
        cond = {"field": "done", "op": "eq", "value": False}
        assert _check_filter(cond, task, None, STICKER_MAP) is False

    def test_done_missing_field(self):
        """Task without 'completed' field defaults to False."""
        task = {}
        cond = {"field": "done", "op": "eq", "value": False}
        assert _check_filter(cond, task, None, STICKER_MAP) is True


# ═══════════════════════════════════════════════════════════════
# _evaluate_chain — AND/OR/NOT operators
# ═══════════════════════════════════════════════════════════════

class TestEvaluateChain:
    def test_single_filter_true(self):
        chain = [{"kind": "filter", "field": "done", "op": "eq", "value": False}]
        task = {"completed": False}
        assert _evaluate_chain(chain, task, None, STICKER_MAP) is True

    def test_single_filter_false(self):
        chain = [{"kind": "filter", "field": "done", "op": "eq", "value": True}]
        task = {"completed": False}
        assert _evaluate_chain(chain, task, None, STICKER_MAP) is False

    def test_and_both_true(self):
        chain = [
            {"kind": "filter", "field": "column", "op": "eq", "value": "col-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "done", "op": "eq", "value": False},
        ]
        task = {"columnId": "col-1", "completed": False}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is True

    def test_and_one_false(self):
        chain = [
            {"kind": "filter", "field": "column", "op": "eq", "value": "col-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "done", "op": "eq", "value": True},
        ]
        task = {"columnId": "col-1", "completed": False}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is False

    def test_or_one_true(self):
        chain = [
            {"kind": "filter", "field": "title", "op": "contains", "value": "фасад"},
            {"kind": "op", "value": "OR"},
            {"kind": "filter", "field": "sticker_name", "op": "contains", "value": "ORWOOD"},
        ]
        task = {"title": "ЛКПФ Фасад", "stickers": {"u1": "abc123"}}
        assert _evaluate_chain(chain, task, None, STICKER_MAP) is True

    def test_or_both_false(self):
        chain = [
            {"kind": "filter", "field": "title", "op": "contains", "value": "бетон"},
            {"kind": "op", "value": "OR"},
            {"kind": "filter", "field": "sticker_name", "op": "contains", "value": "бетон"},
        ]
        task = {"title": "Фасад", "stickers": {}}
        assert _evaluate_chain(chain, task, None, STICKER_MAP) is False

    def test_not_excludes(self):
        chain = [
            {"kind": "filter", "field": "board", "op": "eq", "value": "board-1"},
            {"kind": "op", "value": "NOT"},
            {"kind": "filter", "field": "done", "op": "eq", "value": True},
        ]
        # board matches AND task is NOT done → True
        task = {"columnId": "col-1", "completed": False}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is True

    def test_not_excludes_match(self):
        chain = [
            {"kind": "filter", "field": "board", "op": "eq", "value": "board-1"},
            {"kind": "op", "value": "NOT"},
            {"kind": "filter", "field": "done", "op": "eq", "value": True},
        ]
        # board matches BUT task IS done → False (NOT condition fires)
        task = {"columnId": "col-1", "completed": True}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is False

    def test_empty_chain(self):
        assert _evaluate_chain([], {}, None, {}) is False

    def test_three_filters_and(self):
        chain = [
            {"kind": "filter", "field": "board", "op": "eq", "value": "board-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "column", "op": "eq", "value": "col-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "done", "op": "eq", "value": False},
        ]
        task = {"columnId": "col-1", "completed": False}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is True

    def test_board_column_done_chain(self):
        """Realistic: board=board-1 AND column=col-1 AND done=false."""
        chain = [
            {"kind": "filter", "field": "board", "op": "eq", "value": "board-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "column", "op": "eq", "value": "col-1"},
            {"kind": "op", "value": "AND"},
            {"kind": "filter", "field": "done", "op": "eq", "value": False},
        ]
        task = {"columnId": "col-1", "completed": False}
        assert _evaluate_chain(chain, task, SAMPLE_BOARD, STICKER_MAP) is True


# ═══════════════════════════════════════════════════════════════
# _rule_to_chain — legacy format conversion
# ═══════════════════════════════════════════════════════════════

class TestRuleToChain:
    def test_chain_passthrough(self):
        chain = [{"kind": "filter", "field": "done", "op": "eq", "value": False}]
        rule = {"type": "chain", "chain": chain}
        assert _rule_to_chain(rule) == chain

    def test_composite_and(self):
        rule = {
            "type": "composite",
            "operator": "AND",
            "conditions": [
                {"field": "board", "op": "eq", "value": "b1"},
                {"field": "done", "op": "eq", "value": False},
            ],
        }
        chain = _rule_to_chain(rule)
        assert len(chain) == 3
        assert chain[0]["field"] == "board"
        assert chain[1]["value"] == "AND"
        assert chain[2]["field"] == "done"

    def test_column_watch(self):
        rule = {"type": "column_watch", "board_id": "b1", "column_id": "c1"}
        chain = _rule_to_chain(rule)
        assert len(chain) == 3  # board + AND + column
        assert chain[0]["field"] == "board"
        assert chain[2]["field"] == "column"

    def test_keyword_filter(self):
        rule = {
            "type": "keyword_filter",
            "title_keywords": ["фасад", "столешка"],
            "sticker_keywords": ["ORWOOD"],
        }
        chain = _rule_to_chain(rule)
        # Should have: title(фасад) OR title(столешка) OR sticker_name(ORWOOD)
        filter_fields = [c for c in chain if c.get("kind") == "filter"]
        assert len(filter_fields) == 3
